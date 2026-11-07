// The app's own commands get permissions, so each window can be given only what it needs:
// the app's screens get all of them, the local search page only `open_path`.
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
    ])))
    .expect("failed to run tauri-build");
}
