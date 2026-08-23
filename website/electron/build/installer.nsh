; Kiro Crew's Windows installer remains an electron-builder assisted NSIS
; installer. This include keeps electron-builder's extraction, update, UAC,
; registry, shortcut, and uninstall machinery while presenting a compact,
; native Windows 11 wizard: welcome, install scope, ready, progress, finish.

!include LogicLib.nsh
!include FileFunc.nsh
!include WinMessages.nsh
!include nsDialogs.nsh
!include x64.nsh
!include installer-messages.nsh

!define KIRO_PREF_DESKTOP "KiroInstallerDesktopShortcut"
!define KIRO_PREF_STARTUP "KiroInstallerStartWithWindows"
!define KIRO_RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define KIRO_DWMWA_USE_IMMERSIVE_DARK_MODE 20
!define KIRO_PBM_SETBARCOLOR 0x0409
!define KIRO_PBM_SETBKCOLOR 0x2001
!define KIRO_FILE_ATTRIBUTE_DIRECTORY 0x10
!define KIRO_INVALID_FILE_ATTRIBUTES -1

!ifndef BUILD_UNINSTALLER

!include StrFunc.nsh
${StrRep}

Var KiroTheme
Var KiroPage
Var KiroPrimaryFont
Var KiroTitleFont
Var KiroPrimaryColor
Var KiroMutedColor
Var KiroWindowBackground
Var KiroSurfaceBackground
Var KiroSelectedBackground
Var KiroScope
Var KiroCurrentRadio
Var KiroAllRadio
Var KiroCurrentCard
Var KiroAllCard
Var KiroCurrentUserLabel
Var KiroAllUsersLabel
Var KiroLocationInput
Var KiroBrowseButton
Var KiroDesktopCheckbox
Var KiroStartupCheckbox
Var KiroCreateDesktopShortcut
Var KiroStartWithWindows
Var KiroInstallDir
Var KiroPerUserDefault
Var KiroPerMachineDefault
Var KiroHasPerUserInstallation
Var KiroHasPerMachineInstallation
Var KiroSkipOptions
Var KiroNativeNext
Var KiroNativeBack
Var KiroNativeCancel
Var KiroProgressPage
Var KiroProgressBar

Function KiroDetectTheme
  StrCpy $KiroTheme "light"
  ClearErrors
  ReadRegDWORD $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" "AppsUseLightTheme"
  ${IfNot} ${Errors}
  ${AndIf} $0 == 0
    StrCpy $KiroTheme "dark"
  ${EndIf}

  ${If} $KiroTheme == "dark"
    StrCpy $KiroPrimaryColor 0xF7F3FB
    StrCpy $KiroMutedColor 0xC7BECF
    StrCpy $KiroWindowBackground 0x201C24
    StrCpy $KiroSurfaceBackground 0x2A2530
    StrCpy $KiroSelectedBackground 0x352B43
  ${Else}
    StrCpy $KiroPrimaryColor 0x241E2B
    StrCpy $KiroMutedColor 0x6A626F
    StrCpy $KiroWindowBackground 0xFFFFFF
    StrCpy $KiroSurfaceBackground 0xF8F6FA
    StrCpy $KiroSelectedBackground 0xF3ECFF
  ${EndIf}
FunctionEnd

Function KiroStyleNativeControl
  Exch $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  ${If} $KiroTheme == "dark"
    System::Call 'uxtheme::SetWindowTheme(p r0, w "DarkMode_Explorer", p 0)i'
  ${Else}
    System::Call 'uxtheme::SetWindowTheme(p r0, w "Explorer", p 0)i'
  ${EndIf}
  SetCtlColors $0 $KiroPrimaryColor $KiroSurfaceBackground
  Pop $0
FunctionEnd

Function KiroStyleLabel
  Exch $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $0 $KiroPrimaryColor transparent
  Pop $0
FunctionEnd

Function KiroStyleMutedLabel
  Exch $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $0 $KiroMutedColor transparent
  Pop $0
FunctionEnd

Function KiroStyleNavigation
  GetDlgItem $KiroNativeNext $HWNDPARENT 1
  GetDlgItem $KiroNativeCancel $HWNDPARENT 2
  GetDlgItem $KiroNativeBack $HWNDPARENT 3
  Push $KiroNativeNext
  Call KiroStyleNativeControl
  Push $KiroNativeCancel
  Call KiroStyleNativeControl
  Push $KiroNativeBack
  Call KiroStyleNativeControl
FunctionEnd

Function KiroApplyWindowTheme
  Call KiroDetectTheme
  CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 9 500
  StrCpy $5 0
  ${If} $KiroTheme == "dark"
    StrCpy $5 1
  ${EndIf}
  System::Call "dwmapi::DwmSetWindowAttribute(p $HWNDPARENT, i ${KIRO_DWMWA_USE_IMMERSIVE_DARK_MODE}, *i r5, i 4)i"
  Call KiroStyleNavigation
FunctionEnd

Function KiroRefreshScopeCards
  ${If} $KiroCurrentCard == 0
  ${OrIf} $KiroAllCard == 0
    Return
  ${EndIf}
  ${If} $KiroScope == "all"
    SetCtlColors $KiroCurrentCard $KiroPrimaryColor $KiroSurfaceBackground
    SetCtlColors $KiroAllCard $KiroPrimaryColor $KiroSelectedBackground
  ${Else}
    SetCtlColors $KiroCurrentCard $KiroPrimaryColor $KiroSelectedBackground
    SetCtlColors $KiroAllCard $KiroPrimaryColor $KiroSurfaceBackground
  ${EndIf}
FunctionEnd

Function KiroUseCurrentUser
  StrCpy $KiroScope "current"
  StrCpy $KiroInstallDir $KiroPerUserDefault
  GetDlgItem $KiroNativeNext $HWNDPARENT 1
  SendMessage $KiroNativeNext ${BCM_SETSHIELD} 0 0
  ${If} $KiroLocationInput != 0
    ${NSD_SetText} $KiroLocationInput $KiroInstallDir
    EnableWindow $KiroLocationInput 1
  ${EndIf}
  ${If} $KiroBrowseButton != 0
    EnableWindow $KiroBrowseButton 1
  ${EndIf}
  Call KiroRefreshScopeCards
FunctionEnd

Function KiroUseAllUsers
  StrCpy $KiroScope "all"
  StrCpy $KiroInstallDir $KiroPerMachineDefault
  GetDlgItem $KiroNativeNext $HWNDPARENT 1
  System::Call "shell32::IsUserAnAdmin()i.r0"
  ${If} $0 == 0
    SendMessage $KiroNativeNext ${BCM_SETSHIELD} 0 1
  ${Else}
    SendMessage $KiroNativeNext ${BCM_SETSHIELD} 0 0
  ${EndIf}
  ${If} $KiroLocationInput != 0
    ${NSD_SetText} $KiroLocationInput $KiroInstallDir
    EnableWindow $KiroLocationInput 0
  ${EndIf}
  ${If} $KiroBrowseButton != 0
    EnableWindow $KiroBrowseButton 0
  ${EndIf}
  Call KiroRefreshScopeCards
FunctionEnd

Function KiroSelectCurrentUser
  Pop $0
  ${NSD_Check} $KiroCurrentRadio
  ${NSD_Uncheck} $KiroAllRadio
  Call KiroUseCurrentUser
FunctionEnd

Function KiroSelectAllUsers
  Pop $0
  ${NSD_Uncheck} $KiroCurrentRadio
  ${NSD_Check} $KiroAllRadio
  Call KiroUseAllUsers
FunctionEnd

Function KiroWelcomeShow
  Call KiroApplyWindowTheme
FunctionEnd

Function KiroSetHeaderText
  Exch $1
  Exch
  Exch $0
  GetDlgItem $2 $HWNDPARENT 1037
  SendMessage $2 ${WM_SETTEXT} 0 "STR:$0"
  GetDlgItem $2 $HWNDPARENT 1038
  SendMessage $2 ${WM_SETTEXT} 0 "STR:$1"
  Pop $0
  Pop $1
FunctionEnd

Function KiroScopeCreate
  ${If} $KiroSkipOptions == 1
    Abort
  ${EndIf}
  Call KiroApplyWindowTheme
  Push "$(chooseInstallationOptions)"
  Push "$(whoShouldThisApplicationBeInstalledFor)"
  Call KiroSetHeaderText
  nsDialogs::Create 1018
  Pop $KiroPage
  ${If} $KiroPage == error
    Abort
  ${EndIf}

  CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 9 500
  CreateFont $KiroTitleFont "Segoe UI Variable Display" 17 650
  ; Dialog backgrounds use the CTLCOLORDLG brush, so leave its unused text
  ; color empty and set the page brush explicitly for both system themes.
  SetCtlColors $KiroPage "" $KiroWindowBackground

  ${NSD_CreateLabel} 8u 6u 284u 25u "$(whoShouldThisApplicationBeInstalledFor)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroTitleFont 0
  SetCtlColors $0 $KiroPrimaryColor transparent

  ${NSD_CreateLabel} 8u 34u 284u 24u "$(selectUserMode)"
  Pop $0
  Push $0
  Call KiroStyleMutedLabel

  ${NSD_CreateGroupBox} 8u 62u 284u 47u ""
  Pop $KiroCurrentCard
  SetCtlColors $KiroCurrentCard $KiroPrimaryColor $KiroSelectedBackground
  ${NSD_CreateRadioButton} 20u 70u 250u 15u "$KiroCurrentUserLabel"
  Pop $KiroCurrentRadio
  Push $KiroCurrentRadio
  Call KiroStyleLabel
  ${NSD_OnClick} $KiroCurrentRadio KiroSelectCurrentUser
  ${NSD_CreateLabel} 39u 87u 236u 14u "$(freshInstallForCurrent)"
  Pop $0
  Push $0
  Call KiroStyleMutedLabel

  ${NSD_CreateGroupBox} 8u 116u 284u 47u ""
  Pop $KiroAllCard
  SetCtlColors $KiroAllCard $KiroPrimaryColor $KiroSurfaceBackground
  ${NSD_CreateRadioButton} 20u 124u 250u 15u "$KiroAllUsersLabel"
  Pop $KiroAllRadio
  Push $KiroAllRadio
  Call KiroStyleLabel
  ${NSD_OnClick} $KiroAllRadio KiroSelectAllUsers
  ${NSD_CreateLabel} 39u 141u 236u 14u "$(freshInstallForAll)"
  Pop $0
  Push $0
  Call KiroStyleMutedLabel

  ${If} $KiroScope == "all"
    ${NSD_Check} $KiroAllRadio
    Call KiroUseAllUsers
  ${Else}
    ${NSD_Check} $KiroCurrentRadio
    Call KiroUseCurrentUser
  ${EndIf}
  ${NSD_SetFocus} $KiroCurrentRadio
  nsDialogs::Show
FunctionEnd

Function KiroScopeLeave
  ${NSD_GetState} $KiroAllRadio $0
  ${If} $0 == ${BST_CHECKED}
    Call KiroUseAllUsers
  ${Else}
    Call KiroUseCurrentUser
  ${EndIf}
  StrCpy $KiroCurrentCard 0
  StrCpy $KiroAllCard 0
FunctionEnd

Function KiroLocationChanged
  Pop $0
  ${NSD_GetText} $KiroLocationInput $KiroInstallDir
FunctionEnd

Function KiroBrowseClicked
  Pop $0
  nsDialogs::SelectFolderDialog "$(^DirBrowseText)" "$KiroInstallDir"
  Pop $1
  ${If} $1 != "error"
  ${AndIf} $1 != ""
    StrCpy $KiroInstallDir $1
    ClearErrors
    Call KiroEnsureAppInstallDir
    ${If} ${Errors}
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(^DirBrowseText)"
      ${NSD_SetFocus} $KiroLocationInput
      Return
    ${EndIf}
    ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${EndIf}
FunctionEnd

Function KiroReadyCreate
  ${If} $KiroSkipOptions == 1
    Abort
  ${EndIf}
  Call KiroApplyWindowTheme
  Push "$(kiroReadyToInstall)"
  Push "$(kiroInstallOptions)"
  Call KiroSetHeaderText
  nsDialogs::Create 1018
  Pop $KiroPage
  ${If} $KiroPage == error
    Abort
  ${EndIf}

  CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 9 500
  CreateFont $KiroTitleFont "Segoe UI Variable Display" 17 650
  SetCtlColors $KiroPage "" $KiroWindowBackground

  ${NSD_CreateLabel} 8u 6u 284u 25u "$(kiroReadyToInstall)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroTitleFont 0
  SetCtlColors $0 $KiroPrimaryColor transparent

  ${NSD_CreateLabel} 8u 39u 74u 14u "$(kiroInstallFor)"
  Pop $0
  Push $0
  Call KiroStyleMutedLabel
  ${If} $KiroScope == "all"
    StrCpy $0 "$KiroAllUsersLabel"
  ${Else}
    StrCpy $0 "$KiroCurrentUserLabel"
  ${EndIf}
  ${NSD_CreateLabel} 88u 39u 204u 14u "$0"
  Pop $0
  Push $0
  Call KiroStyleLabel

  ${NSD_CreateLabel} 8u 65u 74u 14u "$(kiroInstallLocation)"
  Pop $0
  Push $0
  Call KiroStyleMutedLabel
  ${NSD_CreateText} 88u 60u 158u 20u "$KiroInstallDir"
  Pop $KiroLocationInput
  Push $KiroLocationInput
  Call KiroStyleNativeControl
  ${NSD_OnChange} $KiroLocationInput KiroLocationChanged
  ${NSD_CreateBrowseButton} 251u 60u 41u 20u "$(^BrowseBtn)"
  Pop $KiroBrowseButton
  Push $KiroBrowseButton
  Call KiroStyleNativeControl
  ${NSD_OnClick} $KiroBrowseButton KiroBrowseClicked

  ${NSD_CreateCheckbox} 88u 91u 204u 18u "$(kiroDesktopShortcut)"
  Pop $KiroDesktopCheckbox
  Push $KiroDesktopCheckbox
  Call KiroStyleLabel
  ${If} $KiroCreateDesktopShortcut == 1
    ${NSD_Check} $KiroDesktopCheckbox
  ${EndIf}

  ${NSD_CreateCheckbox} 88u 114u 204u 18u "$(kiroStartWithWindows)"
  Pop $KiroStartupCheckbox
  Push $KiroStartupCheckbox
  Call KiroStyleLabel
  ${If} $KiroStartWithWindows == 1
    ${NSD_Check} $KiroStartupCheckbox
  ${EndIf}

  ${NSD_CreateLabel} 8u 148u 284u 18u "$(kiroReadyToInstall)"
  Pop $0
  Push $0
  Call KiroStyleMutedLabel

  ${If} $KiroScope == "all"
    Call KiroUseAllUsers
  ${Else}
    Call KiroUseCurrentUser
  ${EndIf}
  GetDlgItem $KiroNativeNext $HWNDPARENT 1
  SendMessage $KiroNativeNext ${WM_SETTEXT} 0 "STR:$(kiroInstallAction)"
  ${NSD_SetFocus} $KiroNativeNext
  nsDialogs::Show
FunctionEnd

; electron-builder's generated uninstaller removes $INSTDIR recursively. A
; fresh install therefore owns only a directory that did not exist before the
; install. Normalize to a product-name leaf, then keep nesting past collisions;
; checking only the leaf name would mistake an unrelated existing folder named
; Kiro Crew for an install root. Updates skip this function, so legacy custom
; paths stay in place.
Function KiroEnsureAppInstallDir
  ; A manual installer launch over an existing registration is still an
  ; update, even without electron-updater's --updated flag. Keep its registered
  ; root exactly as-is instead of treating it as a fresh directory collision.
  ${If} $KiroScope == "current"
  ${AndIf} $KiroHasPerUserInstallation == 1
    Return
  ${EndIf}
  ${If} $KiroScope == "all"
  ${AndIf} $KiroHasPerMachineInstallation == 1
    Return
  ${EndIf}

  ; GetFileName treats an existing file like a directory leaf. Walk back to
  ; the nearest existing ancestor and reject it unless it is a directory;
  ; otherwise both a direct file destination and a missing child below a file
  ; can reach electron-builder even though the target can never be created.
  StrCpy $2 $KiroInstallDir
  KiroCheckExistingInstallParent:
  System::Call 'kernel32::GetFileAttributesW(w "$2")i.r0'
  ${If} $0 != ${KIRO_INVALID_FILE_ATTRIBUTES}
    IntOp $1 $0 & ${KIRO_FILE_ATTRIBUTE_DIRECTORY}
    ${If} $1 == 0
      SetErrors
      Return
    ${EndIf}
    Goto KiroExistingInstallParentReady
  ${EndIf}
  ${GetParent} "$2" $3
  ${If} $3 == ""
  ${OrIf} $3 == $2
    Goto KiroExistingInstallParentReady
  ${EndIf}
  StrCpy $2 $3
  Goto KiroCheckExistingInstallParent

  KiroExistingInstallParentReady:

  ${GetFileName} "$KiroInstallDir" $0
  ${If} $0 != "${APP_FILENAME}"
    StrCpy $KiroInstallDir "$KiroInstallDir\${APP_FILENAME}"
  ${EndIf}

  KiroCheckFreshInstallDir:
  IfFileExists "$KiroInstallDir\*.*" KiroFreshInstallDirExists 0
  IfFileExists "$KiroInstallDir" KiroFreshInstallDirExists KiroFreshInstallDirReady

  KiroFreshInstallDirExists:
  StrCpy $KiroInstallDir "$KiroInstallDir\${APP_FILENAME}"
  Goto KiroCheckFreshInstallDir

  KiroFreshInstallDirReady:
FunctionEnd

Function KiroReadyLeave
  ${NSD_GetText} $KiroLocationInput $KiroInstallDir
  ${If} $KiroScope == "all"
    ; Reassert the protected machine location at the elevation boundary. UI
    ; state is not a security boundary and must not be trusted here.
    StrCpy $KiroInstallDir $KiroPerMachineDefault
  ${EndIf}
  ${If} $KiroInstallDir == ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(^DirBrowseText)"
    Abort
  ${EndIf}
  ClearErrors
  Call KiroEnsureAppInstallDir
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(^DirBrowseText)"
    ${NSD_SetFocus} $KiroLocationInput
    Abort
  ${EndIf}
  ${NSD_GetState} $KiroDesktopCheckbox $KiroCreateDesktopShortcut
  ${NSD_GetState} $KiroStartupCheckbox $KiroStartWithWindows
  StrCpy $INSTDIR $KiroInstallDir

  ${If} $KiroScope == "all"
    System::Call "shell32::IsUserAnAdmin()i.r0"
    ${If} $0 == 0
      StrCpy $0 "/allusers /kiro-options /kiro-desktop=$KiroCreateDesktopShortcut /kiro-startup=$KiroStartWithWindows /D=$KiroInstallDir"
      ClearErrors
      ExecShell "runas" "$EXEPATH" "$0"
      ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "$(loginWithAdminAccount)"
        Abort
      ${EndIf}
      Quit
    ${EndIf}
  ${EndIf}
  StrCpy $KiroLocationInput 0
  StrCpy $KiroBrowseButton 0
FunctionEnd

; electron-builder's generated install-mode page parses /D after the custom
; ready page. Reapply the protected destination at that boundary so an external
; command-line path cannot replace the app-owned directory that the uninstaller
; is later allowed to remove recursively.
Function KiroApplyOptions
  ${If} $KiroScope == "all"
    StrCpy $KiroInstallDir $KiroPerMachineDefault
  ${EndIf}
  ${If} $KiroSkipOptions == 0
  ${OrIf} $KiroScope == "all"
    ClearErrors
    Call KiroEnsureAppInstallDir
    ${If} ${Errors}
      ${IfNot} ${Silent}
        MessageBox MB_OK|MB_ICONSTOP "$(^DirBrowseText)"
      ${EndIf}
      SetErrorLevel 2
      Quit
    ${EndIf}
  ${EndIf}
  StrCpy $INSTDIR $KiroInstallDir
  Abort
FunctionEnd

Function KiroInstallShow
  Call KiroApplyWindowTheme
  FindWindow $KiroProgressPage "#32770" "" $HWNDPARENT
  ${If} $KiroProgressPage == 0
    Return
  ${EndIf}
  GetDlgItem $KiroProgressBar $KiroProgressPage 1004
  ${If} $KiroProgressBar != 0
    System::Call 'uxtheme::SetWindowTheme(p $KiroProgressBar, w "", w "")i'
    SendMessage $KiroProgressBar ${KIRO_PBM_SETBARCOLOR} 0 0xDD3D76
    ${If} $KiroTheme == "dark"
      SendMessage $KiroProgressBar ${KIRO_PBM_SETBKCOLOR} 0 0x30252A
    ${Else}
      SendMessage $KiroProgressBar ${KIRO_PBM_SETBKCOLOR} 0 0xF7F3F8
    ${EndIf}
  ${EndIf}
FunctionEnd
!endif

!macro customWelcomePage
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroWelcomeShow
  !insertmacro skipPageIfUpdated
  !insertmacro MUI_PAGE_WELCOME
  Page custom KiroScopeCreate KiroScopeLeave
  Page custom KiroReadyCreate KiroReadyLeave
!macroend

!macro customPageAfterChangeDir
  Page custom KiroApplyOptions
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroInstallShow
!macroend

!macro customInit
  StrCpy $KiroCreateDesktopShortcut 1
  StrCpy $KiroStartWithWindows 0
  StrCpy $KiroScope "current"
  StrCpy $KiroLocationInput 0
  StrCpy $KiroBrowseButton 0
  ${If} $installMode == "all"
    StrCpy $KiroScope "all"
  ${EndIf}
  StrCpy $KiroInstallDir $INSTDIR
  StrCpy $KiroPerUserDefault "$LOCALAPPDATA\Programs\${APP_FILENAME}"
  StrCpy $KiroPerMachineDefault "$PROGRAMFILES\${APP_FILENAME}"
  StrCpy $KiroHasPerUserInstallation 0
  StrCpy $KiroHasPerMachineInstallation 0
  !ifdef APP_64
    ${If} ${RunningX64}
      StrCpy $KiroPerMachineDefault "$PROGRAMFILES64\${APP_FILENAME}"
    ${EndIf}
  !endif
  ${If} $perUserInstallationFolder != ""
    StrCpy $KiroPerUserDefault $perUserInstallationFolder
    StrCpy $KiroHasPerUserInstallation 1
  ${EndIf}
  ${If} $perMachineInstallationFolder != ""
    StrCpy $KiroPerMachineDefault $perMachineInstallationFolder
    StrCpy $KiroHasPerMachineInstallation 1
  ${EndIf}

  ; Existing installs predate startup opt-in, so a missing preference must not
  ; silently opt them in during an update.
  ${If} $KiroHasPerUserInstallation == 1
  ${OrIf} $KiroHasPerMachineInstallation == 1
    StrCpy $KiroStartWithWindows 0
  ${EndIf}
  ClearErrors
  ReadRegDWORD $0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_DESKTOP}"
  ${IfNot} ${Errors}
    StrCpy $KiroCreateDesktopShortcut $0
  ${EndIf}
  ClearErrors
  ReadRegDWORD $0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_STARTUP}"
  ${IfNot} ${Errors}
    StrCpy $KiroStartWithWindows $0
  ${EndIf}

  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/kiro-options" $R1
  ${IfNot} ${Errors}
    StrCpy $KiroSkipOptions 1
    StrCpy $KiroScope "all"
    ; /kiro-options is an untrusted command-line token. Ignore /D and derive
    ; the elevated destination from the protected machine root again.
    StrCpy $KiroInstallDir $KiroPerMachineDefault
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/kiro-desktop=" $R1
  ${IfNot} ${Errors}
    StrCpy $KiroCreateDesktopShortcut $R1
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/kiro-startup=" $R1
  ${IfNot} ${Errors}
    StrCpy $KiroStartWithWindows $R1
  ${EndIf}
  ; A directly launched newer installer is also an update when electron-builder
  ; finds an existing registration. Preserve its scope, path, and preferences
  ; instead of presenting fresh-install choices that could relocate it.
  ${If} $KiroHasPerUserInstallation == 1
  ${OrIf} $KiroHasPerMachineInstallation == 1
    StrCpy $KiroSkipOptions 1
  ${EndIf}
  ${If} ${isUpdated}
    StrCpy $KiroSkipOptions 1
  ${EndIf}

  ; Custom pages do not run during a silent install, so establish the same
  ; ownership boundary here. Preserve a registered update root exactly; for a
  ; fresh per-user /D target, normalize into a previously nonexistent app leaf.
  ; Machine installs always re-derive their destination from Program Files.
  ${If} $KiroScope == "all"
    StrCpy $KiroInstallDir $KiroPerMachineDefault
  ${ElseIf} $KiroHasPerUserInstallation == 1
    StrCpy $KiroInstallDir $KiroPerUserDefault
  ${ElseIf} $KiroInstallDir == ""
    StrCpy $KiroInstallDir $KiroPerUserDefault
  ${EndIf}
  ClearErrors
  Call KiroEnsureAppInstallDir
  ${If} ${Errors}
    ${IfNot} ${Silent}
      MessageBox MB_OK|MB_ICONSTOP "$(^DirBrowseText)"
    ${EndIf}
    SetErrorLevel 2
    Quit
  ${EndIf}
  StrCpy $INSTDIR $KiroInstallDir

  ReadEnvStr $0 "USERNAME"
  ${StrRep} $KiroCurrentUserLabel "$(onlyForMe)" "&" ""
  StrCpy $KiroCurrentUserLabel "$KiroCurrentUserLabel ($0)"
  ${StrRep} $KiroAllUsersLabel "$(forAll)" "&" ""
!macroend

!macro customInstallMode
  !ifndef BUILD_UNINSTALLER
    ${If} $KiroScope == "all"
      StrCpy $isForceMachineInstall 1
    ${Else}
      StrCpy $isForceCurrentInstall 1
    ${EndIf}
  !endif
!macroend

!macro customInstall
  WriteRegDWORD SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_DESKTOP}" $KiroCreateDesktopShortcut
  WriteRegDWORD SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_STARTUP}" $KiroStartWithWindows
  ${If} $KiroCreateDesktopShortcut != 1
    Delete "$newDesktopLink"
  ${EndIf}
  ${If} $KiroStartWithWindows == 1
    WriteRegStr SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}" '"$appExe"'
  ${Else}
    DeleteRegValue SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}"
  ${EndIf}
!macroend

!macro customUnInstall
  DeleteRegValue SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}"
  ; The generated uninstaller clears only Roaming AppData. Remove this channel's
  ; LocalAppData updater cache on a real uninstall, never during an auto-update.
  ${ifNot} ${isUpdated}
    DetailPrint "Removing update cache: $LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
    RMDir /r "$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
  ${endIf}
!macroend
