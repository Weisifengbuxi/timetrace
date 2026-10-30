// TimeTrace recorder watcher: native helper, waits on the tracker process handle.
#![windows_subsystem = "windows"]

use std::ffi::c_void;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const DETACHED_PROCESS: u32 = 0x0000_0008;
const SYNCHRONIZE: u32 = 0x0010_0000;
const WAIT_OBJECT_0: u32 = 0;
const WAIT_TIMEOUT: u32 = 258;
const STALL_SECS: u64 = 15 * 60;
const ACTIVE_IDLE_SECS: u64 = 180;

const NOTE_STOPPED: &str = "\u{8bb0}\u{5f55}\u{5668}\u{505c}\u{6b62}\u{8fd0}\u{884c}";
const NOTE_STARTED: &str = "\u{8bb0}\u{5f55}\u{5668}\u{5f00}\u{59cb}\u{8fd0}\u{884c}";
const NOTE_BOOT: &str = "\u{5f00}\u{673a}\u{81ea}\u{542f}";
const NOTE_MISSING: &str = "\u{8bb0}\u{5f55}\u{5668}\u{6587}\u{4ef6}\u{4e0d}\u{89c1}\u{4e86}";
const NOTE_STALLED: &str = "\u{91c7}\u{96c6}\u{505c}\u{6ede}\u{ff0c}\u{5df2}\u{81ea}\u{52a8}\u{91cd}\u{542f}";

#[repr(C)]
struct ProcessEntry32W {
    dw_size: u32,
    cnt_usage: u32,
    th32_process_id: u32,
    th32_default_heap_id: usize,
    th32_module_id: u32,
    cnt_threads: u32,
    th32_parent_process_id: u32,
    pc_pri_class_base: i32,
    dw_flags: u32,
    sz_exe_file: [u16; 260],
}

#[repr(C)]
struct LastInputInfo {
    cb_size: u32,
    dw_time: u32,
}

#[repr(C)]
struct SysTime {
    year: u16,
    month: u16,
    day_of_week: u16,
    day: u16,
    hour: u16,
    minute: u16,
    second: u16,
    milliseconds: u16,
}

#[link(name = "kernel32")]
extern "system" {
    fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> isize;
    fn Process32FirstW(snap: isize, entry: *mut ProcessEntry32W) -> i32;
    fn Process32NextW(snap: isize, entry: *mut ProcessEntry32W) -> i32;
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> isize;
    fn WaitForSingleObject(handle: isize, millis: u32) -> u32;
    fn CloseHandle(handle: isize) -> i32;
    fn GetTickCount() -> u32;
    fn GetTickCount64() -> u64;
    fn GetLocalTime(st: *mut SysTime);
    fn CreateMutexW(attr: *const c_void, initial: i32, name: *const u16) -> isize;
    fn GetLastError() -> u32;
}

#[link(name = "user32")]
extern "system" {
    fn GetLastInputInfo(info: *mut LastInputInfo) -> i32;
}

fn local_dir() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA").map(PathBuf::from)
}

fn data_dir() -> Option<PathBuf> {
    local_dir().map(|p| p.join("RustTimeNoter"))
}

fn lifecycle_path() -> Option<PathBuf> {
    data_dir().map(|p| p.join("lifecycle.log"))
}

fn state_path() -> Option<PathBuf> {
    local_dir().map(|p| p.join("TimeTrace").join("watch.state"))
}

fn now_string() -> String {
    unsafe {
        let mut st: SysTime = std::mem::zeroed();
        GetLocalTime(&mut st);
        format!(
            "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
            st.year, st.month, st.day, st.hour, st.minute, st.second
        )
    }
}

fn log_event(kind: &str, note: &str) {
    let Some(path) = lifecycle_path() else { return };
    if let Some(dir) = path.parent() {
        let _ = fs::create_dir_all(dir);
    }
    let line = format!(
        "{{\"ts\": \"{}\", \"kind\": \"{}\", \"note\": \"{}\"}}\n",
        now_string(),
        kind,
        note
    );
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&path) {
        let _ = f.write_all(line.as_bytes());
    }
}

fn find_tracker_pid() -> u32 {
    unsafe {
        let snap = CreateToolhelp32Snapshot(0x0000_0002, 0);
        if snap == -1 || snap == 0 {
            return 0;
        }
        let mut entry: ProcessEntry32W = std::mem::zeroed();
        entry.dw_size = std::mem::size_of::<ProcessEntry32W>() as u32;
        let mut found = 0u32;
        if Process32FirstW(snap, &mut entry) != 0 {
            loop {
                let name: String = entry
                    .sz_exe_file
                    .iter()
                    .take_while(|c| **c != 0)
                    .map(|c| char::from_u32(*c as u32).unwrap_or('?'))
                    .collect();
                if name.eq_ignore_ascii_case("tracker.exe") {
                    found = entry.th32_process_id;
                    break;
                }
                if Process32NextW(snap, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snap);
        found
    }
}

fn tracker_exe() -> Option<PathBuf> {
    data_dir().map(|p| p.join("bin").join("tracker.exe"))
}

fn idle_seconds() -> Option<u64> {
    unsafe {
        let mut info = LastInputInfo {
            cb_size: std::mem::size_of::<LastInputInfo>() as u32,
            dw_time: 0,
        };
        if GetLastInputInfo(&mut info) == 0 {
            return None;
        }
        let tick = GetTickCount();
        Some(tick.wrapping_sub(info.dw_time) as u64 / 1000)
    }
}

fn newest_shard_age() -> Option<u64> {
    let root = data_dir()?.join("data");
    let mut newest: u64 = 0;
    let mut stack = vec![root];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = fs::read_dir(&dir) else { continue };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().map(|e| e == "log").unwrap_or(false) {
                if let Ok(meta) = entry.metadata() {
                    if let Ok(modified) = meta.modified() {
                        if let Ok(secs) = modified.duration_since(UNIX_EPOCH) {
                            if secs.as_secs() > newest {
                                newest = secs.as_secs();
                            }
                        }
                    }
                }
            }
        }
    }
    if newest == 0 {
        return None;
    }
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    Some(now.saturating_sub(newest))
}

fn restart_recorder() {
    if let Some(exe) = tracker_exe() {
        let _ = Command::new(exe)
            .creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS)
            .spawn();
    }
}

fn stop_recorder() {
    if let Some(exe) = tracker_exe() {
        let _ = Command::new(exe)
            .arg("stop")
            .creation_flags(CREATE_NO_WINDOW)
            .spawn();
    }
}

fn boot_id() -> u64 {
    let uptime = unsafe { GetTickCount64() } / 1000;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    now.saturating_sub(uptime)
}

fn write_state(running: bool) {
    let Some(path) = state_path() else { return };
    if let Some(dir) = path.parent() {
        let _ = fs::create_dir_all(dir);
    }
    let text = format!(
        "{{\"present\": true, \"running\": {}, \"boot\": {}}}",
        running,
        boot_id()
    );
    let _ = fs::write(path, text);
}

fn previous_boot() -> Option<u64> {
    let text = fs::read_to_string(state_path()?).ok()?;
    let key = "\"boot\": ";
    let idx = text.find(key)? + key.len();
    let rest = &text[idx..];
    let end = rest.find(|c: char| !c.is_ascii_digit()).unwrap_or(rest.len());
    rest[..end].parse().ok()
}

fn single_instance() -> bool {
    let name: Vec<u16> = "TimeTraceWatch_Rust\0".encode_utf16().collect();
    unsafe {
        let handle = CreateMutexW(std::ptr::null(), 0, name.as_ptr());
        if handle == 0 {
            return true;
        }
        GetLastError() != 183
    }
}

fn main() {
    if !single_instance() {
        return;
    }

    let boot = boot_id();
    if let Some(prev) = previous_boot() {
        if prev != boot {
            log_event("started", NOTE_BOOT);
        }
    }
    write_state(true);

    let mut handle: isize = 0;
    let mut known_pid: u32 = 0;
    let mut was_running = true;

    loop {
        let pid = find_tracker_pid();
        if pid != 0 {
            if handle == 0 || pid != known_pid {
                if handle != 0 {
                    unsafe { CloseHandle(handle) };
                }
                handle = unsafe { OpenProcess(SYNCHRONIZE, 0, pid) };
                known_pid = pid;
                if !was_running {
                    log_event("started", NOTE_STARTED);
                    was_running = true;
                    write_state(true);
                }
            }
            if handle != 0 {
                let rc = unsafe { WaitForSingleObject(handle, 60_000) };
                if rc == WAIT_OBJECT_0 {
                    log_event("stopped", NOTE_STOPPED);
                    unsafe { CloseHandle(handle) };
                    handle = 0;
                    known_pid = 0;
                    was_running = false;
                    write_state(false);
                    if tracker_exe().map(|p| !p.exists()).unwrap_or(false) {
                        log_event("missing", NOTE_MISSING);
                    }
                } else if rc == WAIT_TIMEOUT {
                    if let (Some(idle), Some(age)) = (idle_seconds(), newest_shard_age()) {
                        if idle < ACTIVE_IDLE_SECS && age > STALL_SECS {
                            log_event("stalled", NOTE_STALLED);
                            stop_recorder();
                            thread::sleep(Duration::from_secs(2));
                            restart_recorder();
                        }
                    }
                }
            } else {
                thread::sleep(Duration::from_secs(5));
            }
        } else {
            if was_running {
                log_event("stopped", NOTE_STOPPED);
                was_running = false;
                write_state(false);
                if tracker_exe().map(|p| !p.exists()).unwrap_or(false) {
                    log_event("missing", NOTE_MISSING);
                }
            }
            thread::sleep(Duration::from_secs(5));
        }
    }
}
