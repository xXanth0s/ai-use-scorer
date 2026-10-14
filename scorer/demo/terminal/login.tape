# Login beat, terminal half: connect the agent once (real `scorer install` into a throwaway settings
# file), then one sandboxed `claude -p` session whose hooks stream into the bottom pane.
# Re-record: bash demo/terminal/login_record.sh   (from anywhere; writes demo/out/login-term.mp4 + stamps)
Output "/tmp/scorer-login-raw.mp4"
Set Shell bash
Set FontSize 24
Set FontFamily "JetBrains Mono"
Set Width 1920
Set Height 1080
Set Padding 40
Set Framerate 30
Set TypingSpeed 28ms
Set Theme { "name": "sherpa-light", "background": "#fbf6ee", "foreground": "#1c2d22", "cursor": "#1c2d22", "selection": "#ddd9d3", "black": "#1c2d22", "red": "#802d11", "green": "#316c5a", "yellow": "#b36813", "blue": "#0369a1", "magenta": "#7a3e6d", "cyan": "#2f6f73", "white": "#4d5a4d", "brightBlack": "#4d5a4d", "brightRed": "#802d11", "brightGreen": "#316c5a", "brightYellow": "#b36813", "brightBlue": "#0369a1", "brightMagenta": "#7a3e6d", "brightCyan": "#2f6f73", "brightWhite": "#1c2d22" }

Hide
Type "bash demo/terminal/login_setup.sh && tmux attach -t login" Enter
Sleep 3s
Type@1ms "_stamp show; clear" Enter
Show
Sleep 1s

# 1. connect once: hooks merged into the lab settings file (diff printed, then written)
Type "scorer install --agent claude --settings /tmp/scorer-login/.claude/settings.json --no-schedule --yes"
Sleep 400ms
Enter
Wait+Screen@60s /claude: wrote/
Sleep 2500ms
Ctrl+L
Sleep 300ms

# 2. any session from now on is captured: one real agent run, hooks fire below
Type 'claude -p "add a docstring to hello.py, then run it" --settings /tmp/scorer-login/.claude/settings.json --allowedTools Edit "Bash(python*)"'
Sleep 400ms
Enter
Wait+Screen@240s /SessionEnd/
Sleep 3s
Hide
Type@1ms "_stamp end" Enter
Sleep 500ms
