#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
use std::{fs,io::{BufRead,BufReader},path::PathBuf,process::{Child,Command,Stdio},sync::Mutex,time::{Duration,Instant}};
use tauri::Manager;
struct Runtime { child: Mutex<Option<Child>>, stop: PathBuf }
impl Runtime {
    fn stop(&self) {
        let mut guard=self.child.lock().unwrap();
        if let Some(mut child)=guard.take() {
            let _=fs::write(&self.stop,b"stop\n");
            let deadline=Instant::now()+Duration::from_secs(25);
            while Instant::now()<deadline { if child.try_wait().ok().flatten().is_some(){return;}std::thread::sleep(Duration::from_millis(100)); }
            let _=child.kill();let _=child.wait();
        }
    }
}
impl Drop for Runtime { fn drop(&mut self) { self.stop(); } }
fn startup_error(message: &str) {
    eprintln!("{message}");
    #[cfg(windows)] unsafe {
        #[link(name="user32")]
        extern "system" { fn MessageBoxW(window:*mut std::ffi::c_void,text:*const u16,caption:*const u16,kind:u32)->i32; }
        let text:Vec<u16>=format!("无法启动 NornV2 区块链实验室。\n\n{message}\n\n请使用完整安装包，并查看应用目录中的 desktop.log。").encode_utf16().chain(Some(0)).collect();
        let title:Vec<u16>="NornV2 区块链实验室".encode_utf16().chain(Some(0)).collect();
        MessageBoxW(std::ptr::null_mut(),text.as_ptr(),title.as_ptr(),0x10);
    }
}
fn main() {
    let application=tauri::Builder::default().plugin(tauri_plugin_single_instance::init(|app,_,_|{
        if let Some(window)=app.get_webview_window("main"){let _=window.unminimize();let _=window.set_focus();}
    })).setup(|app| {
        // Tauri canonicalizes Windows paths to \\?\ form. MinGW's filesystem stat
        // cannot inspect that form consistently; use the equivalent ordinary path.
        let resources=dunce::simplified(&app.path().resource_dir()?).join("runtime");
        let arguments:Vec<_>=std::env::args_os().collect();
        let data=if let Some(index)=arguments.iter().position(|v|v=="--data") {
            PathBuf::from(arguments.get(index+1).ok_or("--data needs a directory")?)
        } else {app.path().app_local_data_dir()?};
        let data=dunce::simplified(&data).to_path_buf();fs::create_dir_all(&data)?;
        let stop=data.join(format!("gateway-stop-{}",std::process::id()));
        if stop.exists(){fs::remove_file(&stop)?;}
        let error_log=fs::OpenOptions::new().create(true).append(true).open(data.join("desktop.log"))?;
        let mut command=Command::new(resources.join("chain_classroom.exe"));
        command.args(["--data"]).arg(&data).args(["--node"]).arg(resources.join("boost.exe"))
            .args(["--web"]).arg(resources.join("web")).args(["--contract"]).arg(resources.join("ClassroomChat.compiled.json"))
            .args(["--stop-file"]).arg(&stop).arg("--parent-pid").arg(std::process::id().to_string())
            .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::from(error_log));
        #[cfg(windows)] {use std::os::windows::process::CommandExt;command.creation_flags(0x08000000);}
        let mut child=command.spawn()?;let stdout=child.stdout.take().ok_or("Missing gateway output")?;
        let (tx,rx)=std::sync::mpsc::channel();std::thread::spawn(move||{let mut line=String::new();let result=BufReader::new(stdout).read_line(&mut line);let _=tx.send(result.map(|_|line));});
        let line=match rx.recv_timeout(Duration::from_secs(20)){Ok(Ok(s))=>s,_=>{let _=child.kill();let _=child.wait();return Err("本地服务启动失败，请检查应用数据目录中的 desktop.log".into());}};
        let info:serde_json::Value=match serde_json::from_str(&line){Ok(value)=>value,Err(error)=>{let _=child.kill();let _=child.wait();return Err(error.into());}};
        let url=info["url"].as_str().ok_or("Missing local URL")?.parse::<tauri::Url>()?;
        if url.host_str()!=Some("127.0.0.1")||url.scheme()!="http"{let _=child.kill();return Err("Invalid gateway URL".into());}
        let origin=url.origin();app.manage(Runtime{child:Mutex::new(Some(child)),stop});
        tauri::WebviewWindowBuilder::new(app,"main",tauri::WebviewUrl::External(url))
            .title("NornV2 区块链实验室").inner_size(1380.0,900.0).min_inner_size(900.0,680.0)
            .on_navigation(move|next| next.origin()==origin).build()?;
        Ok(())
    }).on_window_event(|window,event|{
        if matches!(event,tauri::WindowEvent::CloseRequested{..}) {window.app_handle().state::<Runtime>().stop();}
    }).build(tauri::generate_context!());
    match application {
        Ok(application)=>application.run(|app,event|{if matches!(event,tauri::RunEvent::Exit){app.state::<Runtime>().stop();}}),
        Err(error)=>startup_error(&error.to_string()),
    }
}
