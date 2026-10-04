# Nexus Instagram Connector

This plugin is a Meta API/webhook integration. It does **not** automate the Instagram website and does not store an Instagram password.

## Configuration

Copy `config/instagram.json.example` to `config/instagram.json` and fill in the values supplied by Meta for your Instagram professional-account integration.

Keep `access_token` and `app_secret` private. Do not put them in prompts or commit them to source control.

## Runtime commands

- `status`
- `enable`
- `disable`
- `set_busy`
- `clear_busy`
- `start_webhook`
- `stop_webhook`
- `send_test`

The intended workflow is:

1. Configure the Meta application and Instagram messaging permissions.
2. Point Meta's webhook subscription at a publicly reachable HTTPS endpoint that forwards to Nexus's `/` webhook listener (or deploy the listener directly behind HTTPS).
3. Verify the webhook using the configured verify token.
4. Enable the connector.
5. Say `set Instagram busy` to activate the automatic reply.

The local listener defaults to `127.0.0.1:8787`, so it is not publicly reachable by itself. Use a proper HTTPS reverse proxy/deployment for production rather than exposing the Python development listener directly to the internet.
