; ============================================================================
; Hermes Desktop installer — prerequisite detection page
; ============================================================================
;
; A native NSIS Wizard page (using nsDialogs) inserted between the directory
; selection page and the install-files page. Detects Python 3.11+ and Git
; for Windows; offers to install missing prereqs via winget.
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
; the Page directive so the optimizer doesn't strip them. customInstall has
; a defensive runtime reference too, in case the customPageAfterChangeDir
; hook isn't expanded by some future electron-builder version.
;
; UAC behavior:
;   Python is installed with --scope user (no UAC prompt).
;   Git for Windows always installs per-machine and triggers a UAC prompt.
;   We pre-warn the user via the page footer; the UAC dialog may appear
;   behind the installer, so BringToFront is called after each winget run.
;
; Detection:
;   Python: try `py -3.11`, `py -3.12`, `py -3.13`, `py -3.14` in order.
;   The Python launcher returns exit 0 only when that specific version is
;   installed. The Microsoft Store "Python stub" doesn't install py.exe,
;   so users with only the stub get correctly classified as not-installed.
;
;   Git: `where git` returns exit 0 if git is on PATH.
;
;   winget: `where winget` returns exit 0 on Win11 / Win10 1809+ with App
;   Installer. If unavailable, the page shows manual download URLs.
;
; Skip behaviors:
;   - Both prereqs already installed → page is auto-skipped via Abort
;   - Silent install (/S) → customInstall winget block skips
;   - User unchecks both checkboxes → page advances without running winget
; ============================================================================

!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!include "WinMessages.nsh"

Var HermesDialog
Var HermesPyStatusLabel
Var HermesPyCheckbox
Var HermesGitStatusLabel
Var HermesGitCheckbox
Var HermesFooterLabel
Var HermesHasWinget
Var HermesHasPython
Var HermesHasGit
Var HermesInstallPython
Var HermesInstallGit

; ----------------------------------------------------------------------------
; HermesDetectPrereqs — populates $HermesHasWinget / $HermesHasPython /
; $HermesHasGit with "0" or "1". Called from the page-create function.
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

  ; --- Python 3.11+ ---
  ; The py launcher returns exit 0 only when that specific version is
  ; installed. We probe each version Hermes' pyproject.toml accepts.
  StrCpy $HermesHasPython "0"
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
      ${Else}
        nsExec::Exec 'cmd.exe /c py -3.14 --version >nul 2>&1'
        Pop $0
        ${If} $0 == 0
          StrCpy $HermesHasPython "1"
        ${EndIf}
      ${EndIf}
    ${EndIf}
  ${EndIf}

  ; --- Git ---
  nsExec::Exec 'cmd.exe /c where git >nul 2>&1'
  Pop $0
  ${If} $0 == 0
    StrCpy $HermesHasGit "1"
  ${Else}
    StrCpy $HermesHasGit "0"
  ${EndIf}
FunctionEnd

; ----------------------------------------------------------------------------
; HermesPrereqPageCreate — builds the prereq page UI. If both prereqs are
; already installed we Abort, which causes NSIS to skip directly to the next
; page in the sequence (InstFiles).
; ----------------------------------------------------------------------------
Function HermesPrereqPageCreate
  Call HermesDetectPrereqs

  ${If} $HermesHasPython == "1"
  ${AndIf} $HermesHasGit == "1"
    Abort
  ${EndIf}

  nsDialogs::Create 1018
  Pop $HermesDialog
  ${If} $HermesDialog == error
    Abort
  ${EndIf}

  StrCpy $HermesInstallPython "0"
  StrCpy $HermesInstallGit "0"

  ; Page title (bold) and subtitle. We can't use MUI_HEADER_TEXT here —
  ; electron-builder's NSIS template configures MUI internally but doesn't
  ; expose the header-text macros to user includes. So we render our own
  ; title in the page body using a label with bold font.
  ${NSD_CreateLabel} 0u 0u 100% 10u "System Requirements"
  Pop $0
  CreateFont $1 "$(^Font)" "10" "700"
  SendMessage $0 ${WM_SETFONT} $1 0

  ${NSD_CreateLabel} 0u 12u 100% 18u "Hermes Agent needs Python 3.11+ and Git for Windows to run. Items already installed are listed as detected; missing items can be installed automatically."
  Pop $0

  ; --- Python panel ---
  ${NSD_CreateGroupBox} 0u 34u 100% 32u "Python 3.11+"
  Pop $0
  ${If} $HermesHasPython == "1"
    ${NSD_CreateLabel} 8u 46u 95% 12u "Detected on your system."
    Pop $HermesPyStatusLabel
  ${Else}
    ${If} $HermesHasWinget == "1"
      ${NSD_CreateLabel} 8u 44u 95% 9u "Not detected."
      Pop $HermesPyStatusLabel
      ${NSD_CreateCheckbox} 8u 54u 95% 10u "Install Python 3.11 (per-user install, no admin prompt)"
      Pop $HermesPyCheckbox
      ${NSD_Check} $HermesPyCheckbox
    ${Else}
      ${NSD_CreateLabel} 8u 44u 95% 20u "Not detected. Install manually from https://www.python.org/downloads/ and re-run this installer."
      Pop $HermesPyStatusLabel
    ${EndIf}
  ${EndIf}

  ; --- Git panel ---
  ${NSD_CreateGroupBox} 0u 70u 100% 32u "Git for Windows  (provides Git Bash)"
  Pop $0
  ${If} $HermesHasGit == "1"
    ${NSD_CreateLabel} 8u 82u 95% 12u "Detected on your system."
    Pop $HermesGitStatusLabel
  ${Else}
    ${If} $HermesHasWinget == "1"
      ${NSD_CreateLabel} 8u 80u 95% 9u "Not detected. Required by Hermes' terminal tool."
      Pop $HermesGitStatusLabel
      ${NSD_CreateCheckbox} 8u 90u 95% 10u "Install Git for Windows (administrator approval required)"
      Pop $HermesGitCheckbox
      ${NSD_Check} $HermesGitCheckbox
    ${Else}
      ${NSD_CreateLabel} 8u 80u 95% 20u "Not detected. Install manually from https://git-scm.com/download/win and re-run this installer."
      Pop $HermesGitStatusLabel
    ${EndIf}
  ${EndIf}

  ; --- Footer (UAC notice when Git install will run) ---
  ${If} $HermesHasGit == "0"
  ${AndIf} $HermesHasWinget == "1"
    ${NSD_CreateLabel} 0u 108u 100% 30u "Note: installing Git for Windows requires administrator approval. The User Account Control prompt may appear behind this window — use the taskbar to find it if needed."
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
