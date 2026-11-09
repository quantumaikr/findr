; findr keeps its index, models and engine runtime in %LOCALAPPDATA%\findr (not in the app's own data folder):
; when the person ticks 'delete the application data' in the uninstaller, that folder goes too. Their files are never touched.
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $DeleteAppDataCheckboxState = 1
    RMDir /r "$LOCALAPPDATA\findr"
  ${EndIf}
!macroend
