{
  "product": {
    "id": "openSUSE_Leap"
  },
  "hostname": {
    "static": "fxroute"
  },
  "localization": {
    "language": "en_US.UTF-8",
    "keyboard": "us",
    "timezone": "Europe/Berlin"
  },
  "user": {
    "fullName": "FXRoute",
    "userName": "fxroute",
    "password": "__FXROUTE_PASSWORD_HASH__",
    "hashedPassword": true
  },
  "root": {
    "sshPublicKey": "__FXROUTE_SSH_PUBLIC_KEY__"
  },
  "network": {
    "connections": [
      {
        "id": "FXRoute Ethernet",
        "method4": "auto",
        "method6": "auto",
        "status": "up",
        "autoconnect": true,
        "persistent": true
      }
    ]
  },
  "storage": {
    "drives": [
      {
        "alias": "boot",
        "partitions": [
          {
            "generate": "default"
          }
        ]
      }
    ],
    "boot": {
      "configure": true,
      "device": "boot"
    }
  },
  "software": {
    "packages": [
      "python3",
      "python313-pip",
      "tar",
      "ca-certificates",
      "iproute2",
      "openssh-server"
    ]
  },
  "bootloader": {
    "timeout": 3
  },
  "questions": {
    "policy": "auto"
  },
  "files": [
    {
      "content": "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n",
      "destination": "/etc/ssh/sshd_config.d/90-fxroute-iso.conf",
      "permissions": "0644"
    },
    {
      "url": "device:/fxroute/source.tar",
      "destination": "/opt/fxroute-iso-source.tar",
      "permissions": "0644"
    },
    {
      "content": "headless\n",
      "destination": "/etc/fxroute-iso-profile",
      "permissions": "0644"
    },
    {
      "url": "device:/fxroute/scripts/first-boot-install.sh",
      "destination": "/usr/local/libexec/fxroute-first-boot-install.sh",
      "permissions": "0755"
    },
    {
      "content": "[Unit]\nDescription=FXRoute first-boot setup\nAfter=network-online.target\nWants=network-online.target\nBefore=display-manager.service\n\n[Service]\nType=oneshot\nExecStart=/usr/local/libexec/fxroute-first-boot-install.sh\nRemainAfterExit=yes\nTimeoutStartSec=2h\n\n[Install]\nWantedBy=multi-user.target\n",
      "destination": "/etc/systemd/system/fxroute-first-boot.service",
      "permissions": "0644"
    }
  ],
  "scripts": {
    "post": [
      {
        "name": "enable-fxroute-first-boot",
        "chroot": true,
        "content": "#!/usr/bin/bash\nset -Eeuo pipefail\ninstall -d -m 755 /etc/systemd/system/multi-user.target.wants\nln -srf /etc/systemd/system/fxroute-first-boot.service /etc/systemd/system/multi-user.target.wants/fxroute-first-boot.service\n"
      }
    ]
  }
}
