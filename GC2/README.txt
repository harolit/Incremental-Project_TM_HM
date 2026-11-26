HTML2EXE v2 - package HTML games for Windows, Linux and Android

SETUP (once)
  1. Install Python 3.11 or 3.12 from python.org (tick "Add python.exe to PATH").
  2. Double-click Start_HTML2EXE.bat (installs what it needs, then opens the app).

USE
  1. Pick your game folder (needs index.html or another start page) or a single .html file.
  2. Set name, window size, optional icon.
  3. Tick the targets you want: Windows, Linux, Android (any combination).
  4. Click BUILD. Results land in the output folder.

WINDOWS (.exe)
  - Must be built on Windows. Uses Edge WebView2 (built into Windows 11 / updated Windows 10).
  - "Single .exe" starts slower; untick for a faster folder build.
  - Some antivirus tools flag PyInstaller exes as false positives.

LINUX (.tar.gz)
  - Built from any OS, no WSL needed. Downloads Electron once (~100 MB) and caches it.
  - On Linux: extract it, then run the file named after your game
    (if needed:  chmod +x "YourGame"  and  ./"YourGame").
  - Linux file names are case-sensitive; the launcher tolerates wrong-case file names,
    but fix them in your game if you can.
  - The Electron sandbox is turned off (it needs a setuid helper that archives can't carry).
    The window only loads your bundled game.
  - Pick "arm64" for ARM machines (Raspberry Pi, ARM laptops).

ANDROID (.apk)
  - Needs: Node.js 20+, JDK 21 (Java 17-23 works; Android Studio's own Java 25 is TOO NEW), Android SDK.
    Install JDK 21 with:  winget install EclipseAdoptium.Temurin.21.JDK
    The app auto-detects them; fix the paths in the Android tab if needed.
  - First build downloads Gradle and Android libraries: expect 10+ minutes and a few GB.
  - If Gradle complains about licenses, accept them once:
      <Android SDK>\cmdline-tools\latest\bin\sdkmanager --licenses
  - The APK is debug-signed: copy it to your phone and allow "install unknown apps".
    Publishing to Google Play needs a release build with your own keystore (not included).
  - Android paths are case-sensitive too: match file-name capitalisation in your game.
  - If JDK/Capacitor versions clash, change "Capacitor major" in the Android tab
    (7 = JDK 21, 6 = JDK 17).

NOTES
  - Saves/localStorage persist per game (Windows: %APPDATA%\<AppName>).
  - Games that need online resources still need internet.
