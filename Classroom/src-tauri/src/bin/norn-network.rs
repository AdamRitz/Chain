// A short-lived helper, never a privileged service. The elevated instance only
// applies/removes a rule for its sibling boost.exe. It never executes that node.
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::{
    env,
    net::IpAddr,
    path::Path,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

fn validate(action: &str, profile: &str, remote: &str) -> Result<(), String> {
    if !["status", "configure", "remove"].contains(&action) {
        return Err("Invalid action".into());
    }
    if !["Private", "Domain", "Public"].contains(&profile) {
        return Err("Invalid network profile".into());
    }
    if remote.len() > 1024 || remote.split(',').count() > 16 {
        return Err("Too many addresses".into());
    }
    for entry in remote.split(',') {
        if entry == "LocalSubnet" {
            continue;
        }
        let parts: Vec<_> = entry.split('/').collect();
        let ip: IpAddr = parts[0]
            .parse()
            .map_err(|_| "Use literal IPv4/IPv6 addresses or CIDR prefixes")?;
        if parts.len() > 2
            || ip.is_unspecified()
            || ip.is_multicast()
            || matches!(ip, IpAddr::V4(a) if a.is_broadcast())
            || matches!(ip, IpAddr::V6(a) if a.to_ipv4_mapped().is_some())
        {
            return Err("Invalid remote address".into());
        }
        if parts.len() == 2 {
            let bits: u8 = parts[1].parse().map_err(|_| "Invalid prefix")?;
            if bits == 0 || bits > if ip.is_ipv4() { 32 } else { 128 } {
                return Err("Invalid prefix; /0 is not allowed".into());
            }
        }
    }
    Ok(())
}
fn literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}
fn script(action: &str, node: &Path, profile: &str, remote: &str) -> String {
    format!(
        r#"$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);
try {{
Import-Module "$PSHOME\Modules\NetSecurity\NetSecurity.psd1" -ErrorAction Stop
Import-Module "$PSHOME\Modules\NetConnection\NetConnection.psd1" -ErrorAction Stop
Import-Module "$PSHOME\Modules\NetTCPIP\NetTCPIP.psd1" -ErrorAction Stop
{policy}
$node={node}; $profile={profile}; $remote={remote};
{operation}
}} catch {{ @{{error=$_.Exception.Message}} | ConvertTo-Json -Compress; exit 1 }}
"#,
        policy = include_str!("../../../scripts/NetworkRules.ps1"),
        node = literal(&node.to_string_lossy()),
        profile = literal(profile),
        remote = literal(remote),
        operation = match action {
            "configure" =>
                "Set-NornNetworkRule $node $profile $remote; @{ok=$true} | ConvertTo-Json -Compress",
            "remove" => "Remove-NornNetworkRule $node; @{ok=$true} | ConvertTo-Json -Compress",
            _ => "Get-NornNetworkStatus $node | ConvertTo-Json -Depth 8 -Compress",
        }
    )
}

#[cfg(windows)]
fn powershell(source: &str) -> Result<Value, String> {
    use std::os::windows::process::CommandExt;
    use windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW;
    let mut directory = [0u16; 32768];
    let length =
        unsafe { GetSystemDirectoryW(directory.as_mut_ptr(), directory.len() as u32) } as usize;
    if length == 0 || length >= directory.len() {
        return Err("Cannot locate Windows system directory".into());
    }
    let system = std::path::PathBuf::from(String::from_utf16_lossy(&directory[..length]));
    let exe = system.join("WindowsPowerShell/v1.0/powershell.exe");
    let encoded = STANDARD.encode(
        source
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    );
    let mut child = Command::new(exe)
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            &encoded,
        ])
        .current_dir(system)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(0x08000000)
        .spawn()
        .map_err(|e| e.to_string())?;
    // Drain pipes concurrently, including verbose policy failures, to avoid pipe deadlock.
    use std::io::Read;
    let mut out = child.stdout.take().unwrap();
    let mut err = child.stderr.take().unwrap();
    let output = std::thread::spawn(move || {
        let mut b = Vec::new();
        out.read_to_end(&mut b).map(|_| b)
    });
    let errors = std::thread::spawn(move || {
        let mut b = Vec::new();
        err.read_to_end(&mut b).map(|_| b)
    });
    let deadline = Instant::now() + Duration::from_secs(45);
    let status = loop {
        if let Some(s) = child.try_wait().map_err(|e| e.to_string())? {
            break s;
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Windows network query timed out; contact your administrator".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    };
    let bytes = output
        .join()
        .map_err(|_| "Network output reader failed")?
        .map_err(|e| e.to_string())?;
    let _ = errors.join();
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| "Windows network query failed or was blocked by policy")?;
    if !status.success() || value.get("error").is_some() {
        return Err(value["error"]
            .as_str()
            .unwrap_or("Firewall operation failed")
            .into());
    }
    Ok(value)
}
#[cfg(not(windows))]
fn powershell(_: &str) -> Result<Value, String> {
    Err("Windows only".into())
}

#[cfg(windows)]
fn elevate(action: &str, profile: &str, remote: &str) -> Result<u32, String> {
    use windows_sys::Win32::{
        Foundation::{CloseHandle, GetLastError},
        System::{
            Com::{
                CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE,
            },
            Threading::{GetExitCodeProcess, WaitForSingleObject, INFINITE},
        },
        UI::Shell::{
            ShellExecuteExW, SEE_MASK_FLAG_NO_UI, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS,
            SHELLEXECUTEINFOW,
        },
    };
    let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
    let executable = wide(
        &env::current_exe()
            .map_err(|e| e.to_string())?
            .to_string_lossy(),
    );
    // validate() restricts these arguments to tokens without spaces or quotes.
    let arguments = wide(&format!("--elevated {action} {profile} {remote}"));
    let verb = wide("runas");
    unsafe {
        let initialized = CoInitializeEx(
            std::ptr::null(),
            (COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) as u32,
        ) >= 0;
        let mut info: SHELLEXECUTEINFOW = std::mem::zeroed();
        info.cbSize = std::mem::size_of_val(&info) as u32;
        info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI;
        info.lpVerb = verb.as_ptr();
        info.lpFile = executable.as_ptr();
        info.lpParameters = arguments.as_ptr();
        info.nShow = 0;
        let result = if ShellExecuteExW(&mut info) == 0 {
            let code = GetLastError();
            if code == 1223 {
                Ok(1223)
            } else {
                Err(format!("Cannot request administrator permission ({code})"))
            }
        } else {
            WaitForSingleObject(info.hProcess, INFINITE);
            let mut code = 1;
            GetExitCodeProcess(info.hProcess, &mut code);
            CloseHandle(info.hProcess);
            Ok(code)
        };
        if initialized {
            CoUninitialize();
        }
        result
    }
}
#[cfg(not(windows))]
fn elevate(_: &str, _: &str, _: &str) -> Result<u32, String> {
    Err("Windows only".into())
}

fn address_key(value: &str) -> String {
    if value == "LocalSubnet" {
        return value.into();
    }
    let parts: Vec<_> = value.split('/').collect();
    let Ok(ip) = parts[0].parse::<IpAddr>() else {
        return value.into();
    };
    let width = if ip.is_ipv4() { 32u32 } else { 128u32 };
    let bits = if parts.len() == 1 {
        width
    } else if let Ok(n) = parts[1].parse::<u32>() {
        n
    } else if let Ok(mask) = parts[1].parse::<std::net::Ipv4Addr>() {
        let n = u32::from(mask);
        let count = n.count_ones();
        if n != u32::MAX.checked_shl(32 - count).unwrap_or(0) {
            return value.into();
        }
        count
    } else {
        return value.into();
    };
    if bits > width {
        return value.into();
    }
    let number = match ip {
        IpAddr::V4(a) => u32::from(a) as u128,
        IpAddr::V6(a) => u128::from(a),
    };
    let masked = number & u128::MAX.checked_shl(width - bits).unwrap_or(0);
    format!("{width}:{masked:x}/{bits}")
}
fn same_rule(status: &Value, profile: &str, remote: &str) -> bool {
    let Some(rules) = status["rules"].as_array() else {
        return false;
    };
    if rules.len() != 1 {
        return false;
    }
    let rule = &rules[0];
    let mut expected: Vec<_> = remote.split(',').map(address_key).collect();
    expected.sort();
    expected.dedup();
    let mut actual: Vec<_> = rule["remote"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(address_key)
                .collect()
        })
        .unwrap_or_default();
    actual.sort();
    actual.dedup();
    rule["enabled"] == "True"
        && rule["action"] == "Allow"
        && rule["direction"] == "Inbound"
        && rule["profile"] == profile
        && ["TCP", "6"].iter().any(|s| rule["protocol"] == *s)
        && rule["local_port"] == json!(["Any"])
        && actual == expected
}
fn run() -> Result<Value, String> {
    let mut args: Vec<_> = env::args().skip(1).collect();
    let elevated = args.first().is_some_and(|s| s == "--elevated");
    if elevated {
        args.remove(0);
    }
    if args.len() > 3 {
        return Err("Too many arguments".into());
    }
    let action = args.first().map(String::as_str).unwrap_or("status");
    let profile = args.get(1).map(String::as_str).unwrap_or("Private");
    let remote = args.get(2).map(String::as_str).unwrap_or("LocalSubnet");
    validate(action, profile, remote)?;
    let exe = dunce::canonicalize(env::current_exe().map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let node = exe
        .parent()
        .ok_or("Missing runtime directory")?
        .join("boost.exe");
    if elevated {
        return powershell(&script(action, &node, profile, remote));
    }
    let mut status = powershell(&script("status", &node, profile, remote))?;
    if action == "status" {
        return Ok(status);
    }
    if (action == "remove" && status["rules"].as_array().is_some_and(Vec::is_empty))
        || (action == "configure" && same_rule(&status, profile, remote))
    {
        status["operation"] = json!("unchanged");
        return Ok(status);
    }
    let exit = elevate(action, profile, remote)?;
    if exit == 1223 {
        status["operation"] = json!("cancelled");
        return Ok(status);
    }
    // Read back as the original user. The elevated process never writes a result
    // file to a caller-controlled path, including on alternate-admin UAC login.
    status = powershell(&script("status", &node, profile, remote))?;
    if exit != 0 {
        status["error"] = json!("Windows 未完成配置。请联系管理员检查权限、组策略或规则冲突。");
    } else if action == "remove" && !status["rules"].as_array().is_some_and(Vec::is_empty) {
        status["error"] = json!("规则仍存在，请联系管理员。");
    } else if action == "configure" && !same_rule(&status, profile, remote) {
        status["error"] = json!("配置后的规则与请求范围不一致，请联系管理员。");
    } else {
        status["operation"] = json!("completed");
    }
    Ok(status)
}
fn main() {
    let result = run().unwrap_or_else(|error| json!({"error":error}));
    println!("{result}");
    if result.get("error").is_some() || result["operation"] == "cancelled" {
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_unbounded_and_command_arguments() {
        for s in [
            "Any",
            "*",
            "0.0.0.0/0",
            "::/0",
            "1.2.3.4;whoami",
            "1.2.3.4\"",
            "LocalSubnet -x",
            "127.1",
            "::ffff:127.0.0.1",
            "224.0.0.1",
            "1.2.3.4/33",
            "",
        ] {
            assert!(validate("configure", "Private", s).is_err(), "{s}");
        }
    }
    #[test]
    fn accepts_bounded_dual_stack() {
        for s in [
            "LocalSubnet",
            "192.168.1.0/24",
            "2001:db8::/64",
            "10.1.2.3,2001:db8::1",
        ] {
            assert!(validate("configure", "Public", s).is_ok());
        }
        assert!(validate("shell", "Private", "LocalSubnet").is_err());
        assert!(validate("configure", "Any", "LocalSubnet").is_err());
    }
    #[test]
    fn escapes_paths_as_data() {
        assert_eq!(
            literal("D:\\O'Brien\\boost.exe"),
            "'D:\\O''Brien\\boost.exe'"
        );
    }
    #[test]
    fn windows_address_normalization() {
        assert_eq!(
            address_key("192.168.1.0/24"),
            address_key("192.168.1.0/255.255.255.0")
        );
        assert_eq!(
            address_key("2001:db8::/64"),
            address_key("2001:0db8:0:0::/64")
        );
        assert_ne!(address_key("192.168.1.0/24"), address_key("192.168.1.0/25"));
    }
    #[test]
    fn unchanged_requires_matching_scope() {
        let s = json!({"rules":[{"enabled":"True","action":"Allow","direction":"Inbound","profile":"Private","protocol":"TCP","local_port":["Any"],"remote":["LocalSubnet"]}]});
        assert!(same_rule(&s, "Private", "LocalSubnet"));
        assert!(!same_rule(&s, "Public", "LocalSubnet"));
        assert!(!same_rule(&s, "Private", "10.0.0.0/24"));
    }
}
