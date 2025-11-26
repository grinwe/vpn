# VPN Nodes

## manage_vpn_user.sh

`manage_vpn_user.sh` устанавливается на VPN-ноды в `/usr/local/sbin/manage_vpn_user.sh` и служит для управления учётными записями ShadowTLS + Shadowsocks.

### Команды
- `add-shadowtls-ss <username> <password> <method> <port>` — создаёт файл `/etc/shadowtls-ss/users.d/<username>.conf` с параметрами пользователя и выводит в stdout готовый `ss://` URL.
- `del-shadowtls-ss <username>` — удаляет файл конфигурации пользователя из `/etc/shadowtls-ss/users.d`.

### Примеры
```bash
sudo /usr/local/sbin/manage_vpn_user.sh add-shadowtls-ss alice S3cret chacha20-ietf-poly1305 443
sudo /usr/local/sbin/manage_vpn_user.sh del-shadowtls-ss alice
```
