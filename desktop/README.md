# Windows Launcher

The GitHub Actions workflow builds a single-file `GhostLauncher.exe` and publishes it as the artifact **GhostLauncher-Windows-x64**. The launcher bundles the dashboard and backend, so normal use does not require a separate Node.js or Python installation.

## First run

1. Download `GhostLauncher.exe` from the latest successful **Windows launcher EXE** workflow run.
2. Start the EXE and press **Start / Check Service**.
3. Click **Configure Gemini Key** and enter your Google AI Studio key; it is stored in the Windows OS credential vault, not a plaintext settings file.
4. Make sure Android SDK Platform-Tools (which includes ADB) is installed and available on PATH, then connect your Android phone and approve its USB-debugging authorization prompt. The EXE bundles the app but does not redistribute Google's separate platform-tools package. Select the device serial in the launcher. Official Platform-Tools downloads: https://developer.android.com/tools/releases/platform-tools
5. Enter a task, provider/model, and daily time, then click **Save Daily Task**.
6. Use **Run Now**, **Pause / Enable**, or **Delete** to control the task. The same saved schedules are available through the bundled Control Center.

## Daily automation

The local background service runs the scheduler and keeps saved tasks in the app database. Closing the visible launcher window leaves the service running. Enable **Start automation service when I sign in to Windows** to start the background service after you log into Windows again.

The computer must be awake and signed in at the scheduled time, and the phone must remain connected. A task requiring biometric approval or an unsupported unlock flow can pause for manual help. A failed PIN attempt is not retried automatically.

## Multi-step and repetitive tasks

In the task instructions, describe the finish condition and the order of the steps. For example: “Finish each setup step, verify it on screen, then open the next page. Repeat the safe setup action only while the goal remains incomplete.” The agent checks the visible state after actions, advances after a stage is verified, and reports a blocker rather than looping when repeated attempts make no progress. One run is bounded by the agent's tool-turn limit, so exceptionally long workflows may need to be split into saved tasks.

The agent does not fabricate activity or automate rewarded-ad viewing/clicking or reward-claim loops intended to simulate human engagement.

## Local files

On Windows, user settings, the database, and logs are kept under:

`%LOCALAPPDATA%\GhostInTheDroid`

The Gemini key is stored in Windows Credential Manager. The launcher never asks you to put the phone PIN into a task prompt; if you enable the separate device-unlock capability, its enrollment prompt is hidden and its PIN is stored in the OS credential vault.
