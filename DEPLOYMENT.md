# FXRoute Deployment

This note is for maintainers and contributors, not normal end users.

If you just want to install FXRoute on a machine, use:

```bash
./install.sh
```

## Deploy script

Use the deploy script to copy a full project tree consistently:

```bash
./deploy.sh
```

Useful variants:

```bash
./deploy.sh --dry-run
./deploy.sh --restart
./deploy.sh --delete
```

Override target details as needed:

```bash
DEPLOY_HOST=user@host ./deploy.sh --restart
./deploy.sh --host user@host --dir /home/user/fxroute --service fxroute
```

If you keep project-specific defaults inside `deploy.sh`, treat them as local convenience values rather than public documentation.
