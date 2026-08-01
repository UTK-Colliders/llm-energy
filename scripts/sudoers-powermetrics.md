# Password-less powermetrics

`powermetrics` requires root. To let the harness start/stop it without an
interactive password prompt (needed because measurements run unattended),
install a narrowly-scoped sudoers rule.

Copy-paste:

```sh
printf '%s ALL=(root) NOPASSWD: /usr/bin/powermetrics\n' "$(whoami)" \
  | sudo tee /etc/sudoers.d/powermetrics >/dev/null
sudo chmod 440 /etc/sudoers.d/powermetrics
sudo visudo -c -f /etc/sudoers.d/powermetrics   # must print "parsed OK"
```

Or edit it by hand:

```sh
sudo visudo -f /etc/sudoers.d/powermetrics
```

Add (replace `yourusername` with `whoami` output):

```
yourusername ALL=(root) NOPASSWD: /usr/bin/powermetrics
```

Either way this grants password-less sudo for the powermetrics binary only.
Verify with:

```sh
sudo -n powermetrics --samplers cpu_power -n 1 -i 200 >/dev/null && echo ok
```

Alternative without a sudoers rule: run `sudo -v` immediately before
`llm-energy baseline` / `llm-energy run-task` to cache credentials; note the
default sudo timeout (5 min) must exceed your run duration, so a sudoers rule
is strongly recommended for the MadGraph task.
