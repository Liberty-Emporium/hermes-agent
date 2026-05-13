; ============================================================================
; Hermes Desktop installer — prerequisite detection page
; ============================================================================
;
; A native NSIS Wizard page (using nsDialogs) inserted between the directory
; selection page and the install-files page. Detects Python 3.11+, Git for
; Windows, and ripgrep; offers to install missing items via winget.
;
; Page sequence:
;   Welcome → Directory → [PrereqPage] → InstFiles → Finish
;
; Hooks used:
;   customPageAfterChangeDir — page declaration (electron-builder's hook for
;                              inserting a page between Directory and InstFiles)
;   customInstall            — execute winget for any prereqs the user
;                              checked on the page
;
; The Function declarations live at top-level in this file so they're parsed
; at include time; the customPageAfterChangeDir macro references them via
; the Page directive so the optimizer doesn't strip them.
;
; UAC behavior:
;   Python: --scope user, no UAC.
;   ripgrep: --scope user, no UAC.
;   Git for Windows: always per-machine, triggers UAC prompt.
;   Footer warns the user about Git's UAC; ExecShellWait preserves the
;   foreground focus chain so the prompt comes to front.
;
; Detection:
;   Python: try `py -3.11`/`-3.12`/`-3.13`/`-3.14`. The Python launcher
;     returns exit 0 only when that specific version is installed. The
;     Microsoft Store "Python stub" doesn't install py.exe, so users with
;     only the stub get correctly classified as not-installed.
;   Git: `where git` returns exit 0 if git is on PATH.
;   ripgrep: `where rg` returns exit 0 if rg is on PATH.
;   winget: `where winget` returns exit 0 on Win11 / Win10 1809+ with App
;     Installer. If unavailable, the page shows manual download URLs.
;
; Required vs. recommended:
;   Python and Git are REQUIRED — without them the agent's runtime + terminal
;   tool fail. The page emphasizes "required" wording and the bootstrapper
;   throws if either is missing at first launch.
;   ripgrep is RECOMMENDED — Hermes' search_files tool uses it for fast
;   .gitignore-aware search, and falls back to grep/find from Git Bash when
;   missing (works but slower, less filtering). Page wording is softer for
;   ripgrep so users understand they CAN skip it.
;
; Skip behaviors:
;   - All three already detected → page is auto-skipped via Abort
;   - Silent install (/S) → customInstall winget block skips
;   - User unchecks all checkboxes → page advances without running winget
; ============================================================================

!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!include "WinMessages.nsh"

Var HermesDialog
Var HermesPyStatusLabel
Var HermesPyCheckbox
Var HermesGitStatusLabel
Var HermesGitCheckbox
Var HermesRgStatusLabel
Var HermesRgCheckbox
Var HermesFooterLabel
Var HermesHasWinget
Var HermesHasPython
Var HermesHasGit
Var HermesHasRipgrep
Var HermesInstallPython
Var HermesInstallGit
Var HermesInstallRipgrep

; ----------------------------------------------------------------------------
; HermesDetectPythonViaRegistry — sets $HermesHasPython="1" if a PEP 514
; entry exists for any of the supported Python versions. Reads HKLM
; (system-wide installs) then HKCU (per-user installs). Vendor "PythonCore"
; covers official python.org distributions; "ContinuumAnalytics" covers
; Anaconda/Miniconda. We don't enumerate other vendors because they're
; rare in our user base and we'd rather miss them and let winget add a
; second Python than misclassify something else as a working Python.
; ----------------------------------------------------------------------------
Function HermesDetectPythonViaRegistry
  Push $1
  Push $2

  ; Set view to 64-bit on x64 systems so we read the right hive — the
  ; default 32-bit view would miss a 64-bit Python install on 64-bit
  ; Windows. SetRegView 32 restored at function exit.
  SetRegView 64

  ; Iterate the supported versions. Each is its own ReadRegStr — NSIS
  ; doesn't have loops over arrays inside functions easily, and four
  ; copies is clearer than gymnastics with $R0-$R9.
  ReadRegStr $1 HKLM "SOFTWARE\Python\PythonCore\3.11\InstallPath" ""
  ${If} $1 != ""
    StrCpy $HermesHasPython "1"
    Goto hermes_py_reg_done
  ${EndIf}
  ReadRegStr $1 HKLM "SOFTWARE\Python\PythonCore\3.12\InstallPath" ""
  ${If} $1 != ""
    StrCpy $HermesHasPython "1"
    Goto hermes_py_reg_done
  ${EndIf}
  ReadRegStr $1 HKLM "SOFTWARE\Python\PythonCore\3.13\InstallPath" ""
  ${If} $1 != ""
    StrCpy $HermesHasPython "1"
    Goto hermes_py_reg_done
  ${EndIf}

  ReadRegStr $1 HKCU "SOFTWARE\Python\PythonCore\3.11\InstallPath" ""
  ${If} $1 != ""
    StrCpy $HermesHasPython "1"
    Goto hermes_py_reg_done
  ${EndIf}
  ReadRegStr $1 HKCU "SOFTWARE\Python\PythonCore\3.12\InstallPath" ""
  ${If} $1 != ""
    StrCpy $HermesHasPython "1"
    Goto hermes_py_reg_done
  ${EndIf}
  ReadRegStr $1 HKCU "SOFTWARE\Python\PythonCore\3.13\InstallPath" ""
  ${If} $1 != ""
    StrCpy $HermesHasPython "1"
    Goto hermes_py_reg_done
  ${EndIf}

hermes_py_reg_done:
  SetRegView 32
  Pop $2
  Pop $1
FunctionEnd

; ----------------------------------------------------------------------------
; HermesDetectPythonViaFilesystem — sets $HermesHasPython="1" if a Python
; install exists at one of the standard locations. FileExists never runs
; the binary so this is safe even if the user has the MS Store stub on
; their PATH. We probe both system-wide (Program Files) and per-user
; (LocalAppData\Programs) install locations for versions 3.11–3.14.
; ----------------------------------------------------------------------------
Function HermesDetectPythonViaFilesystem
  ; System-wide installs (default location for python.org with admin)
  ${If} ${FileExists} "$PROGRAMFILES64\Python311\python.exe"
    StrCpy $HermesHasPython "1"
    Return
  ${EndIf}
  ${If} ${FileExists} "$PROGRAMFILES64\Python312\python.exe"
    StrCpy $HermesHasPython "1"
    Return
  ${EndIf}
  ${If} ${FileExists} "$PROGRAMFILES64\Python313\python.exe"
    StrCpy $HermesHasPython "1"
    Return
  ${EndIf}

  ; Per-user installs (default location for python.org without admin
  ; or with "Install for me only"). Covers the user-reported case.
  ${If} ${FileExists} "$LOCALAPPDATA\Programs\Python\Python311\python.exe"
    StrCpy $HermesHasPython "1"
    Return
  ${EndIf}
  ${If} ${FileExists} "$LOCALAPPDATA\Programs\Python\Python312\python.exe"
    StrCpy $HermesHasPython "1"
    Return
  ${EndIf}
  ${If} ${FileExists} "$LOCALAPPDATA\Programs\Python\Python313\python.exe"
    StrCpy $HermesHasPython "1"
    Return
  ${EndIf}
FunctionEnd

; ----------------------------------------------------------------------------
; HermesDetectPrereqs — populates $HermesHasWinget / $HermesHasPython /
; $HermesHasGit / $HermesHasRipgrep with "0" or "1". Called from the
; page-create function.
; ----------------------------------------------------------------------------
Function HermesDetectPrereqs
  ; --- winget ---
  nsExec::Exec 'cmd.exe /c where winget >nul 2>&1'
  Pop $0
  ${If} $0 == 0
    StrCpy $HermesHasWinget "1"
  ${Else}
    StrCpy $HermesHasWinget "0"
  ${EndIf}

  ; --- Python 3.11 / 3.12 / 3.13 ---
  ; We deliberately accept 3.11–3.13 only and NOT 3.14, because some of
  ; Hermes' transitive deps (notably pywinpty, which carries Rust crates
  ; like windows_x86_64_msvc) don't yet publish 3.14 wheels. Without
  ; wheels, `pip install -e .` falls back to building from sdist, which
  ; needs a Rust toolchain. Users without one see a confusing "could
  ; not compile windows_x86_64_msvc build script" error. install.ps1
  ; sidesteps this by pinning to 3.11 via uv; the desktop installer
  ; can't easily install uv in the same flow yet, so we just refuse to
  ; accept 3.14 as "good" and offer 3.11 via winget instead. Revisit
  ; when 3.14 wheels are widely available across our dep tree.
  ;
  ; Detection strategy, in order from most-precise to least-precise.
  ; Each step uses ONLY operations that don't execute `python.exe`
  ; directly off PATH — running `python` on Windows can open the
  ; Microsoft Store if only the "Python stub" is installed, which is
  ; terrible UX during an installer. We avoid that by:
  ;   (a) launcher checks (py.exe runs no python until -V),
  ;   (b) registry reads (PEP 514, no execution at all),
  ;   (c) filesystem probes via FileExists.
  StrCpy $HermesHasPython "0"

  ; (1) The py launcher. Ships with python.org installer when
  ;     "Install launcher for all users" is checked (default for some
  ;     paths, not for per-user installs without elevation). When
  ;     present, py -3.X --version returns 0 iff that version exists.
  nsExec::Exec 'cmd.exe /c py -3.11 --version >nul 2>&1'
  Pop $0
  ${If} $0 == 0
    StrCpy $HermesHasPython "1"
  ${Else}
    nsExec::Exec 'cmd.exe /c py -3.12 --version >nul 2>&1'
    Pop $0
    ${If} $0 == 0
      StrCpy $HermesHasPython "1"
    ${Else}
      nsExec::Exec 'cmd.exe /c py -3.13 --version >nul 2>&1'
      Pop $0
      ${If} $0 == 0
        StrCpy $HermesHasPython "1"
      ${EndIf}
    ${EndIf}
  ${EndIf}

  ; (2) PEP 514 registry probe. Every standards-compliant Python
  ;     installer registers itself under HKLM or HKCU at
  ;     SOFTWARE\Python\PythonCore\<version>\InstallPath. The MS Store
  ;     stub does NOT register here — so we get a clean signal for
  ;     "real Python is installed" without ever risking the Store
  ;     popup. Covers the case the user reported: per-user Python.org
  ;     install without launcher checkbox, plus Anaconda which writes
  ;     similar keys under a different vendor name.
  ${If} $HermesHasPython == "0"
    Call HermesDetectPythonViaRegistry
  ${EndIf}

  ; (3) Filesystem probe of common install locations. Catches edge
  ;     cases where the installer didn't update the registry (rare
  ;     but possible with hand-extracted Python or some third-party
  ;     installers). We only check standard paths — running anything
  ;     would risk spawning the Store stub.
  ${If} $HermesHasPython == "0"
    Call HermesDetectPythonViaFilesystem
  ${EndIf}

  ; --- Git ---
  nsExec::Exec 'cmd.exe /c where git >nul 2>&1'
  Pop $0
  ${If} $0 == 0
    StrCpy $HermesHasGit "1"
  ${Else}
    StrCpy $HermesHasGit "0"
  ${EndIf}

  ; --- ripgrep ---
  nsExec::Exec 'cmd.exe /c where rg >nul 2>&1'
  Pop $0
  ${If} $0 == 0
    StrCpy $HermesHasRipgrep "1"
  ${Else}
    StrCpy $HermesHasRipgrep "0"
  ${EndIf}
FunctionEnd

; ----------------------------------------------------------------------------
; HermesPrereqPageCreate — builds the prereq page UI. If all three items are
; already installed we Abort, which causes NSIS to skip directly to the next
; page in the sequence (InstFiles).
; ----------------------------------------------------------------------------
Function HermesPrereqPageCreate
  Call HermesDetectPrereqs

  ${If} $HermesHasPython == "1"
  ${AndIf} $HermesHasGit == "1"
  ${AndIf} $HermesHasRipgrep == "1"
    Abort
  ${EndIf}

  ; Set the wizard's standard header (top blue/gradient bar). 1037 is the
  ; title control, 1038 is the subtitle. Without this, the header still
  ; reads "Choose Install Location" left over from the Directory page.
  GetDlgItem $0 $HWNDPARENT 1037
  SendMessage $0 ${WM_SETTEXT} 0 "STR:System Requirements"
  GetDlgItem $0 $HWNDPARENT 1038
  SendMessage $0 ${WM_SETTEXT} 0 "STR:Hermes needs Python 3.11+ and Git for Windows. ripgrep is recommended."

  nsDialogs::Create 1018
  Pop $HermesDialog
  ${If} $HermesDialog == error
    Abort
  ${EndIf}

  StrCpy $HermesInstallPython "0"
  StrCpy $HermesInstallGit "0"
  StrCpy $HermesInstallRipgrep "0"

  ; Page body intro. The wizard's header (set above) shows the title
  ; "System Requirements" and subtitle, so we don't repeat them here —
  ; just one short explanatory line.
  ${NSD_CreateLabel} 0u 0u 100% 16u "Items already installed are listed as detected. Missing items can be installed automatically via winget."
  Pop $0

  ; --- Python panel (REQUIRED) ---
  ${NSD_CreateGroupBox} 0u 18u 100% 30u "Python 3.11+  (required)"
  Pop $0
  ${If} $HermesHasPython == "1"
    ${NSD_CreateLabel} 8u 28u 95% 10u "Detected on your system."
    Pop $HermesPyStatusLabel
  ${Else}
    ${If} $HermesHasWinget == "1"
      ${NSD_CreateLabel} 8u 27u 95% 9u "Not detected."
      Pop $HermesPyStatusLabel
      ${NSD_CreateCheckbox} 8u 37u 95% 9u "Install Python 3.11"
      Pop $HermesPyCheckbox
      ${NSD_Check} $HermesPyCheckbox
    ${Else}
      ${NSD_CreateLabel} 8u 27u 95% 14u "Not detected. Install manually from https://www.python.org/downloads/ and re-run this installer."
      Pop $HermesPyStatusLabel
    ${EndIf}
  ${EndIf}

  ; --- Git panel (REQUIRED) ---
  ${NSD_CreateGroupBox} 0u 50u 100% 30u "Git for Windows  (required, provides Git Bash)"
  Pop $0
  ${If} $HermesHasGit == "1"
    ${NSD_CreateLabel} 8u 60u 95% 10u "Detected on your system."
    Pop $HermesGitStatusLabel
  ${Else}
    ${If} $HermesHasWinget == "1"
      ${NSD_CreateLabel} 8u 59u 95% 9u "Not detected. Required by Hermes' terminal tool."
      Pop $HermesGitStatusLabel
      ${NSD_CreateCheckbox} 8u 69u 95% 9u "Install Git for Windows"
      Pop $HermesGitCheckbox
      ${NSD_Check} $HermesGitCheckbox
    ${Else}
      ${NSD_CreateLabel} 8u 59u 95% 14u "Not detected. Install manually from https://git-scm.com/download/win and re-run this installer."
      Pop $HermesGitStatusLabel
    ${EndIf}
  ${EndIf}

  ; --- ripgrep panel (RECOMMENDED) ---
  ${NSD_CreateGroupBox} 0u 82u 100% 30u "ripgrep  (recommended for fast file search)"
  Pop $0
  ${If} $HermesHasRipgrep == "1"
    ${NSD_CreateLabel} 8u 92u 95% 10u "Detected on your system."
    Pop $HermesRgStatusLabel
  ${Else}
    ${If} $HermesHasWinget == "1"
      ${NSD_CreateLabel} 8u 91u 95% 9u "Not detected. Hermes will fall back to slower grep/find."
      Pop $HermesRgStatusLabel
      ${NSD_CreateCheckbox} 8u 101u 95% 9u "Install ripgrep"
      Pop $HermesRgCheckbox
      ${NSD_Check} $HermesRgCheckbox
    ${Else}
      ${NSD_CreateLabel} 8u 91u 95% 14u "Not detected. Install manually from https://github.com/BurntSushi/ripgrep#installation if you want fast .gitignore-aware search."
      Pop $HermesRgStatusLabel
    ${EndIf}
  ${EndIf}

  ; --- Footer (UAC notice when Git install will run) ---
  ${If} $HermesHasGit == "0"
  ${AndIf} $HermesHasWinget == "1"
    ${NSD_CreateLabel} 0u 116u 100% 18u "Note: Git for Windows requires administrator approval. The UAC prompt may appear behind this window — check your taskbar."
    Pop $HermesFooterLabel
  ${EndIf}

  nsDialogs::Show
FunctionEnd

; ----------------------------------------------------------------------------
; HermesPrereqPageLeave — read checkbox states when the user clicks Next.
; Variables stay at "0" if a checkbox doesn't exist (because the
; corresponding prereq is already installed or winget isn't available).
; ----------------------------------------------------------------------------
Function HermesPrereqPageLeave
  ${If} $HermesHasPython == "0"
  ${AndIf} $HermesHasWinget == "1"
    ${NSD_GetState} $HermesPyCheckbox $HermesInstallPython
  ${EndIf}
  ${If} $HermesHasGit == "0"
  ${AndIf} $HermesHasWinget == "1"
    ${NSD_GetState} $HermesGitCheckbox $HermesInstallGit
  ${EndIf}
  ${If} $HermesHasRipgrep == "0"
  ${AndIf} $HermesHasWinget == "1"
    ${NSD_GetState} $HermesRgCheckbox $HermesInstallRipgrep
  ${EndIf}
FunctionEnd

; ----------------------------------------------------------------------------
; Page declaration — inserted between the Directory page and InstFiles via
; the customPageAfterChangeDir hook (defined in
; node_modules/app-builder-lib/templates/nsis/assistedInstaller.nsh, included
; whenever build.nsis.oneClick=false).
;
; Note: NSIS's optimizer emits "warning 6010: install function ... not
; referenced" for these functions because Page custom directives don't count
; as references in the optimizer's reference-tracking pass. We set
; build.nsis.warningsAsErrors=false in package.json so this warning doesn't
; fail the build. The functions ARE actually called by NSIS at page-display
; time — the optimizer just can't see it statically.
; ----------------------------------------------------------------------------
!macro customPageAfterChangeDir
  Page custom HermesPrereqPageCreate HermesPrereqPageLeave
!macroend

; ----------------------------------------------------------------------------
; customInstall — runs the actual winget commands for whatever prereqs the
; user checked on the page. Output streams to the install progress log.
; ----------------------------------------------------------------------------
!macro customInstall
  ; Skip on silent installs (managed deploys handle prereqs out-of-band).
  IfSilent hermes_prereq_install_done

  ${If} $HermesInstallPython == "1"
    ; Python with --scope user installs to %LOCALAPPDATA%\Programs\Python\
    ; — no UAC, no foreground chain to preserve. nsExec::ExecToLog gives
    ; us live output streaming to the install log.
    DetailPrint "Installing Python 3.11+ via winget (silent per-user install, no admin prompt)..."
    nsExec::ExecToLog 'winget install -e --id Python.Python.3.11 --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements'
    Pop $0
    ${If} $0 != 0
      DetailPrint "Python install via winget exited with code $0."
      MessageBox MB_OK|MB_ICONEXCLAMATION|MB_TOPMOST "Python install via winget did not complete successfully (exit code $0).$\r$\n$\r$\nYou can install Python 3.11+ manually from https://www.python.org/downloads/ after Hermes setup finishes. Hermes will not run until Python is installed."
    ${Else}
      DetailPrint "Python 3.11+ installed successfully."
    ${EndIf}
  ${EndIf}

  ${If} $HermesInstallRipgrep == "1"
    ; ripgrep with --scope user — ~5MB, no UAC needed. Failure is non-fatal:
    ; Hermes' search_files tool falls back to grep/find from Git Bash.
    ; nsExec::ExecToLog streams output to the install log.
    DetailPrint "Installing ripgrep via winget (silent per-user install, no admin prompt)..."
    nsExec::ExecToLog 'winget install -e --id BurntSushi.ripgrep.MSVC --scope user --silent --disable-interactivity --accept-package-agreements --accept-source-agreements'
    Pop $0
    ${If} $0 != 0
      DetailPrint "ripgrep install via winget exited with code $0 (non-fatal — Hermes will fall back to grep/find)."
    ${Else}
      DetailPrint "ripgrep installed successfully."
    ${EndIf}
  ${EndIf}

  ${If} $HermesInstallGit == "1"
    ; Git for Windows always installs per-machine and triggers UAC. We use
    ; ExecShellWait (NSIS's wrapper around Windows ShellExecute) instead of
    ; nsExec::ExecToLog because ShellExecute preserves the foreground focus
    ; chain across non-elevated → elevated process spawns. With nsExec the
    ; intermediate hidden winget.exe breaks that chain and UAC ends up
    ; behind the installer window.
    ;
    ; Trade-off: ExecShellWait doesn't capture output, so winget runs in
    ; its own console window. The console flashes briefly while winget
    ; downloads, then UAC fires for the elevated Git installer with
    ; correct foreground promotion.
    DetailPrint "Installing Git for Windows via winget (UAC prompt will appear)..."
    ExecShellWait "open" "winget" "install -e --id Git.Git --silent --disable-interactivity --accept-package-agreements --accept-source-agreements" SW_SHOWNORMAL

    ; ExecShellWait returns no exit code, so verify by checking the file
    ; system directly. Don't use `where git` — that reads OUR process's
    ; PATH, which was captured at NSIS startup before Git's installer ran
    ; and modified the system PATH. Until we restart, the new PATH isn't
    ; visible to us. Probe Git's standard install locations instead.
    StrCpy $0 "0"  ; "git found" flag
    ${If} ${FileExists} "$PROGRAMFILES64\Git\bin\bash.exe"
      StrCpy $0 "1"
    ${ElseIf} ${FileExists} "$PROGRAMFILES\Git\bin\bash.exe"
      StrCpy $0 "1"
    ${ElseIf} ${FileExists} "$PROGRAMFILES32\Git\bin\bash.exe"
      StrCpy $0 "1"
    ${ElseIf} ${FileExists} "$LOCALAPPDATA\Programs\Git\bin\bash.exe"
      StrCpy $0 "1"
    ${EndIf}

    ${If} $0 == "1"
      DetailPrint "Git for Windows installed successfully."
    ${Else}
      DetailPrint "Git for Windows install did not complete (bash.exe not found at standard install locations)."
      MessageBox MB_OK|MB_ICONEXCLAMATION|MB_TOPMOST "Git for Windows install via winget did not complete successfully.$\r$\n$\r$\nYou can install Git for Windows manually from https://git-scm.com/download/win after Hermes setup finishes. Hermes' terminal tool will not work until Git Bash is available."
    ${EndIf}
  ${EndIf}

  hermes_prereq_install_done:
!macroend
