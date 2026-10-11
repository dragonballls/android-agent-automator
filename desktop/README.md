# Windows Launcher

The GitHub Actions workflow builds a single-file `GhostLauncher.exe` and publishes it as the artifact **GhostLauncher-Windows-x64**. The launcher bundles the dashboard and backend, so normal use does not require a separate Node.js or Python installation. Android SDK Platform-Tools (ADB) is a separate prerequisite and is not bundled.

## First run

1. Download the **GhostLauncher-Windows-x64** ZIP artifact from a successful [Windows launcher EXE workflow run](https://github.com/dragonballls/android-agent-automator/actions/workflows/windows-launcher.yml?query=branch%3Amain). Open the run's **Artifacts** section and download the ZIP.
2. Extract the ZIP to a normal folder you control, then start `GhostLauncher.exe` from that extracted folder. Do not run it from inside the ZIP preview.
3. Press **Start / Check Service** and wait until the launcher reports that the local service is running.
4. Click **Configure Gemini Key** and enter your Google AI Studio key. The input is masked and the key is stored in the Windows OS credential vault, not a plaintext settings file. After saving a new key, click **Restart Service** so the running background service loads it.
5. Install the latest Android SDK Platform-Tools from the [official download page](https://developer.android.com/tools/releases/platform-tools) and make sure `adb` is available on PATH. Choose either:
   - **Wireless:** On Android 11 or later, enable Developer options → **Wireless debugging**. Keep the phone and PC on the same trusted Wi-Fi network, click **Pair over Wi-Fi** in the launcher, and follow its instructions. Android displays a temporary pairing port/code and a separate wireless-debugging connection port; enter the matching values from the phone. The code is used for that attempt only and is not stored or logged.
   - **USB:** Enable Developer options → **USB debugging**, connect a data-capable USB cable, unlock the phone, then approve the computer's USB-debugging prompt on the phone.
6. Click **Refresh Connected Phones**. The device must appear in **Target phones** before you can save a task. If it does not, see [Troubleshooting](TROUBLESHOOTING.md#adb-connection-issues).
7. Before using Gemini, read the privacy note below. For the first test, put the phone on a non-sensitive screen such as the home screen.
8. For a normal saved task, enter an instruction, provider/model, and daily time. In **Target phones**, select one phone or Ctrl/Shift-select multiple connected phones, then click **Save Daily Task**. This creates one enabled daily schedule per selected phone.

## First-device smoke test (read-only)

Run this on **one phone first**, before creating multi-phone schedules:

1. Confirm exactly one intended phone is shown and selected under **Target phones**.
2. Put the phone on a harmless, non-sensitive screen. Avoid banking, payment, health, private-message, password, or account-security screens.
3. Set the task name to `Read-only device check` and use this exact task instruction:

   > Read only the screen context already supplied to you. Report the connected device identifier, the foreground app if visible, and the visible page title or general status. Do not tap, swipe, type, navigate, launch or close apps, unlock the phone, change settings, claim rewards, purchase, send, post, or delete anything. Do not call device-action tools. If the supplied context does not show the foreground app or screen, say that explicitly; do not try to obtain it by interacting with the phone.

4. Save the task with a daily time well outside the time you expect to be testing, select its row in **Saved daily tasks**, and click **Run Now**. Saving creates an enabled recurring daily schedule; **after the run finishes, delete this temporary task** so it cannot run again tomorrow.
5. Check its reported result and task/session logs. A useful pass means the report identifies the foreground app or explicitly states that the needed screen context was unavailable, and the phone was not interacted with. If it reports missing context, check the connection/device list before trying again.

A successful read-only check validates only that particular device, connection, and run. It does not prove that arbitrary apps or workflows will work. After one phone passes, the next step is a harmless two-phone check using the same read-only instruction, with each phone selected and reported independently.

## Privacy and credentials

When Gemini is used, the task prompt, relevant screen/UI-tree context, and tool results are sent to Google's Gemini API. Screen content can reveal sensitive information even when the API key is stored securely. Use a non-sensitive screen for testing and do not run tests against personal or financial screens unless you have reviewed and intentionally authorized that data sharing. Never put API keys or the phone lock-screen PIN in a task prompt, issue, log, or screenshot.

## Daily automation

The local background service runs the scheduler and keeps saved tasks in the app database. Closing the visible launcher window leaves the service running. Enable **Start automation service when I sign in to Windows** to start the background service after you log into Windows again.

The computer must be awake and signed in at the scheduled time, and the phone must remain connected. A task requiring biometric approval or an unsupported unlock flow can pause for manual help. A failed PIN attempt is not retried automatically.

**Phone Link and automation:** Phone Link may remain paired for viewing apps and manual mouse/keyboard interaction. It does not expose a general third-party API for this launcher to take over its control session. The launcher uses ADB for screen reading and input; wireless ADB is a separate, explicitly authorized connection and does not automate or take over Phone Link. Use Android Wireless debugging only on networks you trust, and turn it off when you no longer need it.

## Multi-step and repetitive tasks

In task instructions, describe the finish condition and the order of the steps. The agent checks the visible state after actions, advances after a stage is verified, and reports a blocker rather than looping when repeated attempts make no progress. One run is bounded by the agent's tool-turn limit, so exceptionally long workflows may need to be split into saved tasks.

The agent does not fabricate activity or automate rewarded-ad viewing/clicking or reward-claim loops intended to simulate human engagement.

## Local files

On Windows, user settings, the database, and logs are kept under:

`%LOCALAPPDATA%\\GhostInTheDroid`

The Gemini key is stored in Windows Credential Manager. The launcher never asks you to put the phone PIN into a task prompt; if you enable the separate device-unlock capability, its enrollment prompt is hidden and its PIN is stored in the OS credential vault.
