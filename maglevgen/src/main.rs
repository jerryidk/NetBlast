use std::collections::VecDeque;
use std::env;
use std::process;
use std::time::{Instant, Duration};
use std::convert::TryFrom;
use std::thread;

use byteorder::{ByteOrder, LittleEndian};
use ixy::memory::{alloc_pkt_batch, Mempool, Packet};
use ixy::*;

// number of packets sent simultaneously by our driver
const BATCH_SIZE: usize = 32;
// number of packets in our mempool
const NUM_PACKETS: usize = 2048;
// size of our packets
const PACKET_SIZE: usize = 64;

const NUM_FLOW: u32 = 1 << 20;

const UNIQUENESS_FACTOR: u32 = 10;

pub fn main() {
    // MAGLEV: We will just overwrite the source IP with this, up to 1.4 million
    let mut maglev_seq: u32 = 0;
    let mut seq_count: u32 = 0;

    simple_logger::init().unwrap();

    let mut args = env::args();
    args.next();

    let pci_addr = match args.next() {
        Some(arg) => arg,
        None => {
            eprintln!("Usage: cargo run --example generator <pci bus id>");
            process::exit(1);
        }
    };

    let mut dev = ixy_init(&pci_addr, 1, 1, 0).unwrap();

    #[rustfmt::skip]
    let pkt_data = [
        0x90, 0xe2, 0xba, 0xb5, 0x15, 0x74,         // dst MAC
        0x90, 0xe2, 0xba, 0xb5, 0x13, 0x60,         // src MAC
        // +12
        0x08, 0x00,                                 // ether type: IPv4
        0x45, 0x00,                                 // Version, IHL, TOS
        // +16
        ((PACKET_SIZE - 14) >> 8) as u8,            // ip len excluding ethernet, high byte
        ((PACKET_SIZE - 14) & 0xFF) as u8,          // ip len excluding ethernet, low byte
        // +18
        0x00, 0x00, 0x00, 0x00,                     // id, flags, fragmentation
        0x40, 0x11, 0x00, 0x00,                     // TTL (64), protocol (UDP), checksum
        // +26
        0x0A, 0x00, 0x00, 0x01,                     // src ip (10.0.0.1) <-- mutate
        0x0A, 0x00, 0x00, 0x02,                     // dst ip (10.0.0.2)
        0x00, 0x2A, 0x05, 0x39,                     // src and dst ports (42 -> 1337)
        ((PACKET_SIZE - 20 - 14) >> 8) as u8,       // udp len excluding ip & ethernet, high byte
        ((PACKET_SIZE - 20 - 14) & 0xFF) as u8,     // udp len excluding ip & ethernet, low byte
        0x00, 0x00,                                 // udp checksum, optional
        b'i', b'x', b'y'                            // payload
        // rest of the payload is zero-filled because mempools guarantee empty bufs
    ];

    let pool = Mempool::allocate(NUM_PACKETS, 0).unwrap();

    // pre-fill all packet buffer in the pool with data and return them to the packet pool
    {
        let mut buffer: VecDeque<Packet> = VecDeque::with_capacity(NUM_PACKETS);

        alloc_pkt_batch(&pool, &mut buffer, NUM_PACKETS, PACKET_SIZE);

        for p in buffer.iter_mut() {
            for (i, data) in pkt_data.iter().enumerate() {
                p[i] = *data;
            }
            let checksum = calc_ipv4_checksum(&p[14..14 + 20]);
            // Calculated checksum is little-endian; checksum field is big-endian
            p[24] = (checksum >> 8) as u8;
            p[25] = (checksum & 0xff) as u8;
        }
    }

    let mut dev_stats = Default::default();
    let mut dev_stats_old = Default::default();

    dev.reset_stats();

    dev.read_stats(&mut dev_stats);
    dev.read_stats(&mut dev_stats_old);

    thread::sleep(Duration::from_secs(5));

    let mut buffer: VecDeque<Packet> = VecDeque::with_capacity(BATCH_SIZE);
    let mut rx_buffer: VecDeque<Packet> = VecDeque::with_capacity(BATCH_SIZE);
    let mut time = Instant::now();
    let mut seq_num = 0;
    let mut counter = 0;

    loop {
        // re-fill our packet queue with new packets to send out
        alloc_pkt_batch(&pool, &mut buffer, BATCH_SIZE, PACKET_SIZE);

        // update sequence number of all packets (and checksum if necessary)
        for p in buffer.iter_mut() {
            LittleEndian::write_u32(&mut p[(PACKET_SIZE - 4)..], seq_num);
            seq_num = seq_num.wrapping_add(1);

            // MAGLEV: Overwrite source IP
            p[26..30].copy_from_slice(&maglev_seq.to_be_bytes());
            // LittleEndian::write_u32(&mut p[26..30], maglev_seq);
            if seq_count == UNIQUENESS_FACTOR {
                maglev_seq = (maglev_seq + 1) % NUM_FLOW;
                seq_count = 0;
            } else {
                seq_count += 1;
            }
        }

        dev.tx_batch_busy_wait(0, &mut buffer);

        // Don't really need to check for rx
        /*{
            let num_rx = dev.rx_batch(0, &mut rx_buffer, BATCH_SIZE);
            /*
            if num_rx != 0 {
                // println!("{} packets received", num_rx);
            }
            for p in rx_buffer.iter_mut() {
                // TODO: Check sequence number to identify which
                // set request was stored and which get request was successful
                // println!("RX: {:?}", &p);
            }
            */

            rx_buffer.drain(..);
        }*/

        // don't poll the time unnecessarily
        if counter & 0xfff == 0 {
            let elapsed = time.elapsed();
            let nanos = elapsed.as_secs() * 1_000_000_000 + u64::from(elapsed.subsec_nanos());
            // every second
            if nanos > 1_000_000_000 {
                dev.read_stats(&mut dev_stats);
                dev_stats.print_stats_diff(&*dev, &dev_stats_old, nanos);
                dev_stats_old = dev_stats;

                time = Instant::now();
            }
        }

        counter += 1;
    }
}

/// Calculates IPv4 header checksum
fn calc_ipv4_checksum(ipv4_header: &[u8]) -> u16 {
    assert_eq!(ipv4_header.len() % 2, 0);
    let mut checksum = 0;
    for i in 0..ipv4_header.len() / 2 {
        if i == 5 {
            // Assume checksum field is set to 0
            continue;
        }
        checksum += (u32::from(ipv4_header[i * 2]) << 8) + u32::from(ipv4_header[i * 2 + 1]);
        if checksum > 0xffff {
            checksum = (checksum & 0xffff) + 1;
        }
    }
    !(checksum as u16)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ipv4_checksum() {
        // Test case from the Wikipedia article "IPv4 header checksum"
        assert_eq!(
            calc_ipv4_checksum(
                b"\x45\x00\x00\x73\x00\x00\x40\x00\x40\x11\xb8\x61\xc0\xa8\x00\x01\xc0\xa8\x00\xc7"
            ),
            0xb861
        );
    }
}
