# Hooks firing: event stream (left) next to one real `claude -p` run (right).
Output demo/out/hooks.mp4
Set Shell bash
Set FontSize 24
Set FontFamily "JetBrains Mono"
Set Width 1920
Set Height 1080
Set Padding 40
Set Framerate 30
Set TypingSpeed 35ms
Set Theme { "name": "sherpa-light", "background": "#fbf6ee", "foreground": "#1c2d22", "cursor": "#1c2d22", "selection": "#ddd9d3", "black": "#1c2d22", "red": "#802d11", "green": "#316c5a", "yellow": "#b36813", "blue": "#0369a1", "magenta": "#7a3e6d", "cyan": "#2f6f73", "white": "#4d5a4d", "brightBlack": "#4d5a4d", "brightRed": "#802d11", "brightGreen": "#316c5a", "brightYellow": "#b36813", "brightBlue": "#0369a1", "brightMagenta": "#7a3e6d", "brightCyan": "#2f6f73", "brightWhite": "#1c2d22" }

Hide
Type "bash demo/terminal/setup.sh && tmux attach -t demo" Enter
Sleep 2s
Show
Sleep 1s
Type `PROMPT="add a docstring to hello.py, then run it with python" SCORER_HOME=/tmp/scorer-demo bash scripts/demo_e2e.sh`
Sleep 500ms
Enter
Sleep 62s
