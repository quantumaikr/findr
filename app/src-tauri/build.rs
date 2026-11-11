// The app's own commands get permissions, so each window can be given only what it needs:
// the app's screens get all of them, the local search page only `open_path`.
// Every command of `generate_handler!` must be listed here and allowed in capabilities/default.json: one that is
// not is refused at run time, and the screen that called it fails silently (read_progress was: the first index
// showed a bar that never moved, 0.2.x up to 0.2.19). tests/test_app_commands.py checks the three lists.
fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
        "app_state",
        "setup_runtime",
        "list_roots",
        "start_index",
        "start_engine",
        "stop_engine",
        "open_path",
        "open_data_dir",
        "save_folders",
        "load_folders",
        "uninstall_data",
        "quit_app",
        "upgrade_state",
        "set_upgrade",
        "read_progress",
        "set_pause",
    ])))
    .expect("failed to run tauri-build");
}
