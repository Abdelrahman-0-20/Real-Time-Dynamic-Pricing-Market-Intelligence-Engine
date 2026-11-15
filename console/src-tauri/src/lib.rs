// Pricing Console — thin Tauri shell.
//
// SECURITY MODEL: no custom #[tauri::command] surface at all. The webview's
// only capability is the HTTP plugin scoped to the local Pricing API
// (capabilities/default.json: http://127.0.0.1:8000, deny-by-default).
// The opener plugin from the template was REMOVED (unused = attack surface).
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_http::init())
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
