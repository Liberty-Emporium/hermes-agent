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
  ${NSD_CreateGroupBox} 0u 18u 100% 26u "Python 3.11+  (required)"
  Pop $0
  ${If} $HermesHasPython == "1"
    ${NSD_CreateLabel} 8u 28u 95% 10u "Detected on your system."
    Pop $HermesPyStatusLabel
  ${Else}
    ${If} $HermesHasWinget == "1"
      ${NSD_CreateLabel} 8u 27u 95% 9u "Not detected."
      Pop $HermesPyStatusLabel
      ${NSD_CreateCheckbox} 8u 36u 95% 9u "Install Python 3.11"
      Pop $HermesPyCheckbox
      ${NSD_Check} $HermesPyCheckbox
    ${Else}
      ${NSD_CreateLabel} 8u 27u 95% 14u "Not detected. Install manually from https://www.python.org/downloads/ and re-run this installer."
      Pop $HermesPyStatusLabel
    ${EndIf}
  ${EndIf}

  ; --- Git panel (REQUIRED) ---
  ${NSD_CreateGroupBox} 0u 48u 100% 26u "Git for Windows  (required, provides Git Bash)"
  Pop $0
  ${If} $HermesHasGit == "1"
    ${NSD_CreateLabel} 8u 58u 95% 10u "Detected on your system."
    Pop $HermesGitStatusLabel
  ${Else}
    ${If} $HermesHasWinget == "1"
      ${NSD_CreateLabel} 8u 57u 95% 9u "Not detected. Required by Hermes' terminal tool."
      Pop $HermesGitStatusLabel
      ${NSD_CreateCheckbox} 8u 66u 95% 9u "Install Git for Windows"
      Pop $HermesGitCheckbox
      ${NSD_Check} $HermesGitCheckbox
    ${Else}
      ${NSD_CreateLabel} 8u 57u 95% 14u "Not detected. Install manually from https://git-scm.com/download/win and re-run this installer."
      Pop $HermesGitStatusLabel
    ${EndIf}
  ${EndIf}

  ; --- ripgrep panel (RECOMMENDED) ---
  ${NSD_CreateGroupBox} 0u 78u 100% 26u "ripgrep  (recommended for fast file search)"
  Pop $0
  ${If} $HermesHasRipgrep == "1"
    ${NSD_CreateLabel} 8u 88u 95% 10u "Detected on your system."
    Pop $HermesRgStatusLabel
  ${Else}
    ${If} $HermesHasWinget == "1"
      ${NSD_CreateLabel} 8u 87u 95% 9u "Not detected. Hermes will fall back to slower grep/find."
      Pop $HermesRgStatusLabel
      ${NSD_CreateCheckbox} 8u 96u 95% 9u "Install ripgrep"
      Pop $HermesRgCheckbox
      ${NSD_Check} $HermesRgCheckbox
    ${Else}
      ${NSD_CreateLabel} 8u 87u 95% 14u "Not detected. Install manually from https://github.com/BurntSushi/ripgrep#installation if you want fast .gitignore-aware search."
      Pop $HermesRgStatusLabel
    ${EndIf}
  ${EndIf}

  ; --- Footer (UAC notice when Git install will run) ---
  ${If} $HermesHasGit == "0"
  ${AndIf} $HermesHasWinget == "1"
    ${NSD_CreateLabel} 0u 108u 100% 18u "Note: Git for Windows requires administrator approval. The UAC prompt may appear behind this window — check your taskbar."
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
