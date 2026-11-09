# Live test against a real OrcaSlicer 2.5

`run_live.py` drives stock OrcaSlicer 2.5.0 with the plugin installed, on a test PC, from another machine
through an SSH tunnel. Use a copy of your OrcaSlicer folder so your own setup is only read.

1. Download `OrcaSlicer_Windows_V2.5.0_x64_portable.zip` from the OrcaSlicer 2.5.0 release and unpack it
   (Python is bundled in `python\`).
2. Copy `%APPDATA%\OrcaSlicer` to a test folder, keeping `plugins\` (or a Bambu plug-in dialog blocks
   start-up). A conf from 2.4 shows a one-time "Profile syncing change" dialog: click OK.
3. Put `{"port": 13135, "trace": true}` in
   `<test folder>\orca_plugins\plugin_data\orcaslicer_mcp_plugin\settings.json` BEFORE installing.
4. Install through the GUI (Plugins, arrow, Install local plugin, tick Activate, Yes). A hand-made install
   folder works only if it is named `orcaslicer_mcp_plugin.py` (the installer names the folder after the
   file); any other name gives "Duplicate plugin key" on a later GUI install.
5. Start `orca-slicer.exe --datadir <test folder> <model.stl>` in the desktop session, open a tunnel
   (`ssh -f -N -L 13135:127.0.0.1:13135 <test-pc>`), read the token from `plugin_data\...\token.txt`
   into a file only you can read, then `python -I run_live.py read|write|select`.
6. Close OrcaSlicer, delete the test folders and close the tunnel.

Expected: read 16/16, write 9/9, select 5/5. Unsaved edits make OrcaSlicer's own Transfer/Discard dialog
appear and the request waits for the answer; closing the panel gives 503 paused until play is pressed;
deactivating the plugin or closing the app stops its thread in about 0.1 s.
