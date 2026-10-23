# HDMI profiles and routing

How FXRoute selects and keeps the HDMI transport profile. Output modes and
routing are described in the [manual](../MANUAL.md#53-output-mode-crossover-switch-and-output-routing).

## Profile selection

FXRoute derives the required Stereo, 5.1 or 7.1 transport from the mode and the
highest assigned output, limited by the receiver's reported LPCM channels and
rates (ELD). Stereo normally needs two channels, Stereo + Sub at least 5.1
(default sub on Out 6, LFE), and Surround 5.1 or 7.1 according to the routing.

- Missing LPCM descriptors mean unknown capacity; PipeWire's detected channel
  count is used when available.
- For HDMI, the connected receiver's capacity determines the available routing
  outputs, not the currently active profile.
- An edit upgrades the profile when necessary, but does not immediately
  downshift it. The connection watcher checks every five seconds and waits for
  two consecutive narrower targets before reducing a still-supported profile,
  avoiding profile churn between edits.
- An unsupported profile falls back without that extra delay.

## Stored routing

Routing is stored **per HDMI connector and mode**, shared across transport
profiles; existing profile-specific assignments are retained during migration
rather than overwritten.

First-use defaults follow the actual hardware ports when their full order is
available, otherwise standard ACP positions:

- Front L/R play Main, Center its speaker, and LFE Sub 1.
- Stereo and Stereo + Sub leave the surround speakers Off.
- In a six-channel HDMI layout ACP's single RL/RR pair represents the
  **Side L/R** surround speakers; 7.1 has separate **Rear L/R** and
  **Side L/R** groups. Existing saved Rear assignments stay unchanged.
- HDMI carries one LFE channel, so the defaults never assign a second
  independent sub.

Explicit Side and Back channels in source material retain their positions;
when only one surround group is routed, the other folds into it and uses the
playing group's bass-management settings.

## Profile changes and receiver changes

- Profile changes do not restart the service and preserve an analog input of a
  combined profile, volume and mute.
- After receiver loss or replacement, FXRoute restores a supported profile for
  the connector's current routing instead of selecting another device.
- The watcher also reconciles profiles changed outside FXRoute.
- Connection/profile changes invalidate its ELD cache; an otherwise stable
  receiver is rechecked within 30 seconds.
