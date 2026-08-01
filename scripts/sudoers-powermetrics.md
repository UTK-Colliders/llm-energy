# Password-less powermetrics

`powermetrics` requires root. To let the harness start/stop it without an
interactive password prompt (needed because measurements run unattended),
install a narrowly-scoped sudoers rule:

```sh
sudo visudo -f /etc/sudoers.d/powermetrics
```

Add (replace `yourusername` with `whoami` output):

```
yourusername ALL=(root) NOPASSWD: /usr/bin/powermetrics
```

This grants password-less sudo for the powermetrics binary only.

Alternative without a sudoers rule: run `sudo -v` immediately before
`llm-energy baseline` / `llm-energy run-task` to cache credentials; note the
default sudo timeout (5 min) must exceed your run duration, so a sudoers rule
is strongly recommended for the MadGraph task.
