# VPN Node Helpers

## Shadowsocks user management

`manage_vpn_user.sh` lives at `/usr/local/sbin/manage_vpn_user.sh` on VPN nodes. It keeps
per-user Shadowsocks metadata under `/etc/shadowtls-ss/users.d` (including ready-to-use
`ss://` URLs) while the shared Shadowsocks configuration stays in
`/etc/shadowtls-ss/shadowsocks.json`.

### Usage examples

Add a user (username, password, encryption method, port):

```bash
sudo /usr/local/sbin/manage_vpn_user.sh add-shadowtls-ss alice 'p@ssw0rd!' aes-256-gcm 8443
```

Remove the user mapping:

```bash
sudo /usr/local/sbin/manage_vpn_user.sh del-shadowtls-ss alice
```

Both commands are idempotent and can be run repeatedly. Adding a user rewrites that
user's metadata file, and deleting a user simply removes the corresponding file.
