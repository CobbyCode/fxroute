# Caddy TLS renewal evidence under the tight SELinux policy

Evidence that the tight confined SELinux mode for the optional Caddy proxy
(`fxroute_proxy` mini-module + `httpd_var_lib_t` relabel, see `install.sh`
`prepare_caddy_selinux_policy`) survives real certificate renewal cycles
without `unconfined_t`, without `httpd_can_network_connect`, and without a
service restart.

Observed on the test machine (openSUSE Tumbleweed, SELinux enforcing,
targeted): first automatic renewal on 2026-09-25 02:44:27 CEST, roughly
7 hours after the machine was switched to the confined context
(`system_u:system_r:httpd_t:s0`).

## Journal excerpt (`journalctl -u fxroute-caddy`, trimmed)

```
Sep 25 02:44:27 caddy[1729145]: tls: certificate is in configured renewal window
                                based on expiration date subjects=["fxroute.local"]
                                expiration=1790311245 remaining=14177.78
Sep 25 02:44:27 caddy[1729145]: tls.cache.maintenance: certificate expires soon;
                                queuing for renewal identifiers=["fxroute.local"]
Sep 25 02:44:27 caddy[1729145]: tls.cache.maintenance: certificate expires soon;
                                queuing for renewal identifiers=["192.168.178.104"]
Sep 25 02:44:27 caddy[1729145]: tls.renew: acquiring lock / lock acquired
                                identifier="fxroute.local"
Sep 25 02:44:27 caddy[1729145]: tls.renew: renewing certificate
                                identifier="fxroute.local"
Sep 25 02:44:27 caddy[1729145]: tls.renew: certificate renewed successfully
                                identifier="fxroute.local" issuer="local"
Sep 25 02:44:27 caddy[1729145]: tls: reloading managed certificate
                                identifiers=["fxroute.local"]
Sep 25 02:44:27 caddy[1729145]: tls.cache: replaced certificate in cache
                                subjects=["fxroute.local"] new_expiration=1790340268
Sep 25 02:44:27 caddy[1729145]: tls.renew: certificate renewed successfully
                                identifier="192.168.178.104" issuer="local"
```

Denial check over the whole renewal window:

```
journalctl -u fxroute-caddy --since "2026-09-25 02:40:00" \
  | grep -Eic "denied|permission"   # -> 0
```

## Certificate data

| | before | after renewal |
|---|---|---|
| notBefore | 2026-09-24 16:40:44 UTC | 2026-09-25 00:44:27 UTC |
| notAfter  | 2026-09-25 04:40:44 UTC | 2026-09-25 12:44:27 UTC |

12-hour internal-CA leaf certificates. The new certificate is served live
(`openssl s_client -connect 127.0.0.1:443 -servername fxroute.local`) and the
HTTPS proxy path answered `200` throughout.

## File labels after renewal

New files written by Caddy during renewal are labeled correctly by the
`type_transition httpd_t var_lib_t -> httpd_var_lib_t` rule plus the
persistent `semanage fcontext` entry:

```
system_u:object_r:httpd_var_lib_t:s0 fxroute.local.crt
system_u:object_r:httpd_var_lib_t:s0 fxroute.local.key
system_u:object_r:httpd_var_lib_t:s0 fxroute.local.json
```

## Service health

`ActiveState=active`, `NRestarts=0` before, during, and after the renewal —
the reload happens inside the running process, no restart required. The Caddy
process remained `system_u:system_r:httpd_t:s0` throughout.

## Renewal window math

Caddy renews at roughly one third of the 12 h lifetime remaining (observed
`remaining` ≈ 14178 s ≈ 3 h 56 m). Expected renewal instants are therefore
about 4 hours before each expiry: after the renewal above (expiry
14:44:27 CEST), the next window opens around 10:48 CEST, and subsequent
renewals repeat about every 8 hours.

## Watcher notes (operational lesson)

Renewal detection must poll the real storage path
`/var/lib/fxroute-caddy/caddy/certificates/local/<subject>/*.crt` (note the
`caddy/` component; Caddy uses `$XDG_DATA_HOME/caddy` inside
`/var/lib/fxroute-caddy`). The directories are not readable by the admin
user, so a read-only watcher needs `sudo -n` for its polling. A first watcher
that polled `/var/lib/fxroute-caddy/certificates/...` unprivileged saw
nothing even though the renewal itself was fully successful.

The watcher is kept in the repository as `scripts/watch-caddy-renewal.sh`.
Two further traps it avoids: the renewal evidence block must write the
`journalctl` excerpt to the log file instead of stdout, because a `nohup`
start discards stdout; and the health probe must request one of the named
Caddy sites (e.g. `https://fxroute.local`), since a loopback address gets
no SNI match and therefore no certificate. Both are covered by the
environment overrides used for the smoke test.
