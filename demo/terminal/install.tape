# Closing beat: the install command is typed but never run (it is already installed).
Output demo/out/install.mp4
Set Shell bash
Set FontSize 40
Set FontFamily "JetBrains Mono"
Set Width 1920
Set Height 1080
Set Padding 40
Set Framerate 30
Set TypingSpeed 35ms
Set Theme { "name": "sherpa-light", "background": "#fbf6ee", "foreground": "#1c2d22", "cursor": "#1c2d22", "selection": "#ddd9d3", "black": "#1c2d22", "red": "#802d11", "green": "#316c5a", "yellow": "#b36813", "blue": "#0369a1", "magenta": "#7a3e6d", "cyan": "#2f6f73", "white": "#4d5a4d", "brightBlack": "#4d5a4d", "brightRed": "#802d11", "brightGreen": "#316c5a", "brightYellow": "#b36813", "brightBlue": "#0369a1", "brightMagenta": "#7a3e6d", "brightCyan": "#2f6f73", "brightWhite": "#1c2d22" }

Hide
Type "export PS1='$ ' BASH_SILENCE_DEPRECATION_WARNING=1; clear" Enter
Sleep 500ms
Show
Sleep 800ms
Set TypingSpeed 70ms
Type "uv run scorer install"
Sleep 9s
