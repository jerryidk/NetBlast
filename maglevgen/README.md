# maglevgen

This repo contains a simple traffic generator based on the `generator` example from [ixy.rs](https://github.com/ixy-languages/ixy.rs).
It generates IPv4 UDP packets with varying source IP addresses to simulate multiple flows.

By default, it changes the flow every 10 packets and there are 2^20 unique flows.

## Usage

If your card is at `06:00.0`:

```
sudo maglevgen 0000:06:00.0
```
