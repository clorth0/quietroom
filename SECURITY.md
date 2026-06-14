# Security Policy

## Reporting a vulnerability

Please report security issues privately using GitHub's **"Report a vulnerability"**
button on the [Security tab](https://github.com/clorth0/quietroom/security/advisories),
rather than opening a public issue. Reports are acknowledged as soon as possible.

## Scope and threat model

Quietroom is a self-hosted, receive-only RF survey tool that drives a HackRF One
to help you find hidden transmitters (bugs) in a space you own or are authorized
to sweep (technical surveillance countermeasures). By design it:

- binds to `127.0.0.1` only, and is meant to be reached locally or over
  Tailscale / an SSH tunnel, not exposed directly to the internet;
- has no authentication of its own, so it should not be placed on a public
  interface without your own access controls in front of it;
- invokes local SDR binaries (`hackrf_*`) with fixed argument lists (no shell
  interpolation), and validates socket inputs (frequency ranges, sweep
  parameters) before use.

If you expose it beyond localhost, put it behind your own authentication and
network controls.

## Intended use

Quietroom is **receive-only**. It does not transmit, jam, or interfere with any
signal, and it does not demodulate, decode, or record the content of anyone's
communications. The audio-correlation test compares only the *envelope* (signal
strength over time) of a candidate emitter against a test tone you play in your
own room; it never recovers communications content.

Use it only to sweep spaces you own or have explicit authorization to sweep.
