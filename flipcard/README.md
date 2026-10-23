* put flipper-hinata on your flipper zero https://github.com/yamiYori/flipper-hinata/
* add the following udev rules
```SUBSYSTEM=="hidraw", ATTRS{idVendor}=="f822", ATTRS{idProduct}=="0148", GROUP="users", MODE="0660"
SUBSYSTEM=="usb", ATTRS{idVendor}=="f822", ATTRS{idProduct}=="0148", ENV{ID_MM_DEVICE_IGNORE}="1"```
* put `cardd.py in `~/.local/share/flipcard/`
* put `cardd.service` in `~/.config/systemd/user/`
* run `sudo systemctl daemon-reload`
* run `systemctl --user enable --now cardd.service`

# Konami
* make sure spice api is enabled
* if your spice api config is anything other than port 1337 on localhost and no password go to `~/.config/flipcard/cardd.ini` and fill it in

# Sega 
* build the dll with the following command, too lazy to write for anything other than nixos rn
`nix-shell -p zig --run 'zig cc -target x86_64-windows-gnu -O2 -shared -o aimeio_tap_x64.dll aimeio_tap.c'`
* drop the dll in your `App/bin` folder
* put the following in your `segatools.ini`
```[aimeio]
path=aimeio_tap_x64.dll
tapPath=DEVICE\tap.txt
tapHoldMs=3000```
