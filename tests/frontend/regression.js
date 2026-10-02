/* Dependency-free browser tests. Serve repository root, open /tests/frontend/. */
(async () => {
    const assert = (ok, message) => { if (!ok) throw new Error(message); };
    const pause = (ms = 0) => new Promise(resolve => setTimeout(resolve, ms));
    async function until(predicate, message = "condition did not settle") {
        const end = Date.now() + 3000;
        while (!predicate()) { if (Date.now() > end) throw new Error(message); await pause(10); }
    }
    const [markup, source] = await Promise.all([
        fetch("../../public/index.html").then(r => r.text()),
        fetch("../../public/app.js").then(r => r.text()),
    ]);
    const permissions = ["dashboard.view", "users.view", "users.moderate", "users.delete", "broadcast.view", "broadcast.use"];
    const user = (id = 42, name = "Ali") => ({user_id:id, first_name:name, connected:true, connections_count:1, active_connections:1, protected:false, banned:false});
    const page = items => ({items, total:items.length, has_next:false});
    let frame;
    async function fixture() {
        frame?.remove();
        frame = document.createElement("iframe");
        frame.title = "Isolated real admin panel test fixture";
        document.getElementById("fixture").append(frame);
        const clean = markup.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, "");
        const setup = `
            window.calls=[];window.alerts=[];window.confirmations=[];
            window.Telegram={WebApp:{initData:"",ready(){},expand(){},showAlert(m){alerts.push(m)},showConfirm(m,cb){confirmations.push({message:m,answer:cb})}}};
            window.alert=m=>alerts.push(m);window.prompt=()=>null;window.confirm=()=>false;
            window.handle=()=>({});
            window.fetch=async (path,options={})=>{const call={path:String(path),method:options.method||"GET",body:options.body?JSON.parse(options.body):null};calls.push(call);const data=await handle(call);return {ok:true,status:200,json:async()=>data};};
        `;
        const loaded = new Promise(resolve => { frame.onload = resolve; });
        frame.srcdoc = clean.replace("</body>", `<script>${setup}<\/script><script>${source.replace(/<\/script/gi,"<\\/script")}<\/script></body>`);
        await loaded;
        const w = frame.contentWindow;
        w.eval(`INIT_DATA="test-fixture";MY_ROLE="owner";MY_PERMISSIONS=${JSON.stringify(permissions)};applyRoleUi();`);
        w.handle = call => {
            if (call.path.startsWith("/api/users?")) return page([user()]);
            if (call.path === "/api/moderation") return {banned:[]};
            if (call.path === "/api/broadcast/preview") return {recipients:3, preview_text:"Preview"};
            if (call.path === "/api/broadcast/status") return {running:false,total:3,sent:3,failed:0};
            if (call.path === "/api/broadcast" || call.path === "/api/broadcast/retry") return {total:3,status:{running:true,total:3,sent:0}};
            if (call.path === "/api/stats") return {role:"owner",permissions};
            throw new Error(`Unexpected API call ${call.method} ${call.path}`);
        };
        const $ = id => w.document.getElementById(id);
        return {w,$,calls:w.calls,confirmations:w.confirmations};
    }
    const postCalls = (f,path) => f.calls.filter(c=>c.path===path && c.method==="POST");
    const prepareSend = f => { f.$("broadcast-text").value="Original message"; f.$("btn-broadcast").click(); };
    const tests = [
        ["Ban Cancel never posts moderation", async()=>{
            const f=await fixture(); await f.w.loadUsers();
            f.w.document.querySelector(".ban-user").click(); await pause();
            assert(postCalls(f,"/api/moderation").length===0,"Cancel performed a ban");
            assert(!f.w.document.querySelector(".ban-user").disabled,"Cancel left button locked");
        }],
        ["Send Cancel displays recipient count and sends nothing", async()=>{
            const f=await fixture(); prepareSend(f);
            await until(()=>f.confirmations.length===1);
            assert(f.confirmations[0].message.includes("3"),"Recipient count missing");
            f.confirmations[0].answer(false); await until(()=>!f.$("btn-broadcast").disabled);
            assert(postCalls(f,"/api/broadcast").length===0,"Cancelled send was posted");
        }],
        ["Send uses reviewed snapshot and shares a lock with Retry", async()=>{
            const f=await fixture(); prepareSend(f);
            assert(f.$("btn-broadcast").disabled && f.$("btn-retry").disabled,"Send and Retry were not locked together");
            f.$("btn-broadcast").click(); f.$("btn-retry").click();
            await until(()=>f.confirmations.length===1);
            f.$("broadcast-text").value="Changed after preview";
            f.confirmations[0].answer(true);
            await until(()=>postCalls(f,"/api/broadcast").length===1 && !f.$("btn-broadcast").disabled);
            assert(postCalls(f,"/api/broadcast")[0].body.caption==="Original message","Payload changed after preview");
            assert(postCalls(f,"/api/broadcast/preview").length===1,"Repeated click caused duplicate preview");
            assert(postCalls(f,"/api/broadcast/retry").length===0,"Retry overlapped Send");
        }],
        ["Zero recipients never confirms or starts a broadcast", async()=>{
            const f=await fixture(); const fallback=f.w.handle;
            f.w.handle=c=>c.path==="/api/broadcast/preview"?{recipients:0,preview_text:"Empty"}:fallback(c);
            prepareSend(f); await until(()=>!f.$("btn-broadcast").disabled);
            assert(f.confirmations.length===0 && postCalls(f,"/api/broadcast").length===0,"Empty broadcast started");
        }],
        ["Unchanged users preserve card identity and keyboard focus", async()=>{
            const f=await fixture(); f.w.activateTab("tab-users",{load:false}); await f.w.loadUsers();
            const card=f.w.document.querySelector(".connection-item"); const button=card.querySelector(".copy-id");
            button.focus(); assert(f.w.document.activeElement===button,"Fixture could not focus real button");
            await f.w.loadUsers(false,{silent:true});
            assert(f.w.document.querySelector(".connection-item")===card,"Unchanged card was replaced");
            assert(f.w.document.activeElement===button,"Refresh stole focus");
        }],
        ["Search input invalidates an old response before debounce", async()=>{
            const f=await fixture(); let resolveOld; const fallback=f.w.handle;
            f.w.handle=c=>c.path.startsWith("/api/users?") ? (c.path.includes("search=New")?page([user(77,"New")]):new Promise(r=>{resolveOld=r;})) : fallback(c);
            const old=f.w.loadUsers(); await until(()=>!!resolveOld);
            f.$("search-users").value="New";
            f.$("search-users").dispatchEvent(new f.w.Event("input",{bubbles:true}));
            resolveOld(page([user(66,"Old")])); await old;
            assert(!f.$("users-list").textContent.includes("Old"),"Old result painted during debounce");
            await until(()=>f.$("users-list").textContent.includes("New"));
            assert(!f.$("users-list").textContent.includes("Old"),"Old response replaced search results");
        }],
        ["Older broadcast status cannot relock completed Send/Retry", async()=>{
            const f=await fixture(); const resolvers=[]; const fallback=f.w.handle;
            f.w.handle=c=>c.path==="/api/broadcast/status"?new Promise(r=>resolvers.push(r)):fallback(c);
            const first=f.w.loadBroadcastStatus(); const second=f.w.loadBroadcastStatus();
            await until(()=>resolvers.length>0); await pause();
            if(resolvers.length===1){
                // A single-flight implementation prevents the competing response.
                resolvers[0]({running:false}); await Promise.all([first,second]);
            }else{
                resolvers[1]({running:false}); await second;
                resolvers[0]({running:true}); await first;
            }
            assert(!f.$("btn-broadcast").disabled && !f.$("btn-retry").disabled,"Old running response relocked buttons");
        }],
        ["Initial users failure replaces loading placeholder", async()=>{
            const f=await fixture(); f.w.handle=()=>{throw new Error("Synthetic offline error")};
            await f.w.loadUsers();
            assert(!f.$("users-list").querySelector(".loading-state"),"Loading placeholder survived error");
            assert(f.$("users-list").textContent.includes("Synthetic offline error"),"Inline error absent");
        }],
    ];
    let passed=0;
    for(const [name,run] of tests){
        const item=document.createElement("li");document.getElementById("results").append(item);
        try{await run();passed++;item.className="pass";item.textContent=`PASS — ${name}`;}
        catch(error){item.className="fail";item.textContent=`FAIL — ${name}: ${error.message}`;console.error(error);}
    }
    const failed=tests.length-passed;
    document.getElementById("summary").textContent=`${passed} passed, ${failed} failed`;
    document.getElementById("summary").dataset.complete="true";
    document.getElementById("summary").dataset.failed=String(failed);
    window.testResults={passed,failed,total:tests.length};
})().catch(error=>{document.getElementById("summary").textContent=`HARNESS ERROR: ${error.message}`;console.error(error);});
