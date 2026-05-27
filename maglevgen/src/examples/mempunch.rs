#![feature(const_int_pow)]

use std::collections::VecDeque;
use std::env;
use std::process;
use std::time::{Instant, Duration};
use std::convert::TryFrom;
use std::thread;

use byteorder::{ByteOrder, LittleEndian};
use ixy::memory::{alloc_pkt_batch, Mempool, Packet};
use ixy::*;
use core::arch::x86_64::_rdtsc;
use std::alloc;
use std::alloc::Layout;

extern crate rand;

use rand::Rng;
use rand::distributions::Alphanumeric;

// number of packets sent simultaneously by our driver
const BATCH_SIZE: usize = 32;
// number of packets in our mempool
const NUM_PACKETS: usize = 2048;
// size of our packets
const UDP_PACKET: usize = 42;

const KEY_SIZE: usize = 64;
const VALUE_SIZE: usize = 64;

// Compatible with memcached protocol
// <req_id> <seq_nr> <total_dgram> <rsvd>
const HEADER: usize = 4 + 2 + 2 + 2; // 8 bytes
// <get|set><space>
const COMMAND: usize = 3 + 1; // 4 bytes
// <space><flags><space><end>
const FLAGS_SIZE: usize = 1 + 4 + 4 + 2;
// <key> <flags><2*space><value>
const SET_REQUEST: usize = KEY_SIZE + FLAGS_SIZE + VALUE_SIZE + 2;
// <value> <\r\n>
const GET_REQUEST: usize = VALUE_SIZE + 2;

const GET_PAYLOAD: usize = HEADER + COMMAND + GET_REQUEST;
const SET_PAYLOAD: usize = HEADER + COMMAND + SET_REQUEST;

const SET_PACKET_SIZE: usize = UDP_PACKET + SET_PAYLOAD;
const GET_PACKET_SIZE: usize = UDP_PACKET + GET_PAYLOAD;

// Where in the packet does our payload start?
const PAYLOAD_OFFSET: usize = UDP_PACKET;

const KEYS_TOTAL: usize = (1 << 20) * (16 * 3) / 4;
const KEYS_SET: usize = KEYS_TOTAL / 10;

pub fn get_rdtsc() -> u64 {
    unsafe { _rdtsc() }
}

fn get_rand_string(size: usize) -> String {
    rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(size)
        .collect::<String>()
}

static mut SEED: u64 = 123456789;
static POW: u64 = 2u64.pow(31);

pub fn get_rand() -> u64 {
    unsafe {
        SEED = (1103515245 * SEED + 12345) & (POW - 1);
        SEED
    }
}

type KV = (String, String, bool);

#[derive(Default)]
struct KVStats {
    get_miss: u64,
    set_fail: u64,
    get_sent: u64,
    set_sent: u64,
}

impl KVStats {
    fn new() -> Self {
        Default::default()
    }
}
struct KVStore {
    vec: Vec<KV>,
    get_cursor: usize,
    set_cursor: usize,
    set_ack_cursor: usize,
    get_seq: usize,
    already_set_cursor: usize,
    set_complete: bool,
    stats: KVStats,
}

impl KVStore {
    fn new_with_capacity(num_elements: usize) -> Self {
        let mut vec = unsafe {
            let capacity = num_elements * core::mem::size_of::<KV>();
            // align lookup table at cacheline boundary
            let layout = Layout::from_size_align(capacity, 4096)
                    .map_err(|e| panic!("Layout error: {}", e)).unwrap();

            let buf = unsafe { alloc::alloc(layout) as *mut KV };
            println!("buf {:x?} ", buf);
            let mut v: Vec<KV> = unsafe { Vec::from_raw_parts(buf, num_elements, capacity) };
            v
        };

        Self {
            vec,
            get_cursor: 0,
            set_cursor: 0,
            set_ack_cursor: 0,
            get_seq: 0,
            already_set_cursor: 0,
            set_complete: false,
            stats: KVStats::new(),
        }
    }

    fn populate(&mut self) {
        for i in 0..self.vec.len() {

            self.vec[i] = (get_rand_string(KEY_SIZE),
                            get_rand_string(VALUE_SIZE), false);

        }

        for i in 0..self.vec.len() {
            //println!("K : {}, V : {}", self.vec[i].0, self.vec[i].1);
        }
        println!("KEYS_TOTAL {} KEYS_SET {} SET_PKT_SZ {} GET_PKT_SZ {}",
                                KEYS_TOTAL, KEYS_SET, SET_PACKET_SIZE, GET_PACKET_SIZE);
    }

    /// Get a KV pair for GET requests
    fn get_one_rand(&mut self) -> (String, String) {
        //let mut rand = get_rand() as usize;
        let mut rand = self.get_cursor;
        self.get_cursor = (self.get_cursor + 1) % KEYS_TOTAL;
        let idx = self.get_cursor;

        /*let idx = if self.set_cursor <= 0 {
            0
        } else if self.set_cursor > 0 && self.set_cursor < self.vec.len() {
            rand % self.set_cursor
        } else {
            rand & (self.vec.len() - 1)
        };*/

        //println!("getting {}", idx);

        let (k, v, _) = &self.vec[idx];
        (k.to_string(), v.to_string())
    }

    /// Get a KV pair for GET requests
    fn get_one(&mut self) -> (String, String) {
        let (k, v, _) = &self.vec[self.get_cursor];
        if self.get_seq == 10 {
            self.get_cursor = (self.get_cursor + 1) % self.already_set_cursor;
            self.get_seq = 0;
        } else {
            self.get_seq += 1;
        }
        (k.to_string(), v.to_string())
    }

    /// Get a KV pair for SET requests
    fn set_one(&mut self) -> (String, String, u32) {
        let mut idx = 0;
        // if set is complete, just rotate thro the key list
        // and pick one for set
        if self.set_complete {
            self.set_cursor = (self.set_cursor + 1) % KEYS_TOTAL;
            idx = self.set_cursor;
        } else {
            // special handling. two cases
            // 1) we are not receiving any responses
            //      - Always send key 0
            // 2) we are receiving STORED responses
            //      - Set the key one by one

            // When we are receiving a response, set_ack_cursor points to the next element that was
            // successfully stored.
            if self.set_ack_cursor > 0 {
                self.set_cursor = (self.set_cursor + 1) % KEYS_TOTAL;
                idx = self.set_cursor;
            }
        }

        let (k, v, _) = &self.vec[idx];
        //println!("setting {}", idx);
        (k.to_string(), v.to_string(), idx as u32)
    }

    fn finished_setting(&self) -> bool {
        self.already_set_cursor >= self.vec.len()
    }

    fn construct_set_request(&mut self, pkt_data: *mut u8) {
        let (key, val, req_id) = self.set_one();

        let set_hdr: [u8; HEADER + COMMAND] = [0, 0, 0, 0, //request_id
                0, 0, //seq_nr
                0, 1, //datagram_total
                0, 0, //reserved
                b's', b'e', b't', b' '];

        let mut copy_len = set_hdr.len();

        // Copy header
        unsafe {
            std::ptr::copy(set_hdr.as_ptr(), pkt_data, copy_len);
        }

        let mut offset = copy_len;
        copy_len = key.as_bytes().len();

        // Copy key to set
        unsafe {
            std::ptr::copy(key.as_bytes().as_ptr(), pkt_data.offset(offset as isize), copy_len);
        }

        // Patch request_id
        unsafe {
            std::ptr::copy((&req_id.to_be_bytes()).as_ptr(), pkt_data, 4);
        }

        /*
        let x: String = format!("[{}][{}]", self.set_cursor, self.get_cursor);
        let a = v.len() - x.len();
        v[a..].copy_from_slice(&x.into_bytes());
        */

        let set_body: [u8; FLAGS_SIZE] = [b' ', //space1,
                    0, b' ', 0, b' ',
                    0, 0, 0, 0, //len
                    b'\r', b'\n' //newline1
                    ];

        offset += copy_len;
        copy_len = set_body.len();

        // Copy set flags
        unsafe {
            std::ptr::copy(set_body.as_ptr(), pkt_data.offset(offset as isize), copy_len);
        }

        offset += copy_len;
        copy_len = val.as_bytes().len();

        // Copy value to set
        unsafe {
            std::ptr::copy(val.as_bytes().as_ptr(), pkt_data.offset(offset as isize), copy_len);
        }

        let fin = [b'\r', b'\n'];

        offset += copy_len;
        copy_len = fin.len();

        // Copy end byte
        unsafe {
            std::ptr::copy((&fin).as_ptr(), pkt_data.offset(offset as isize), copy_len);
        }
        /*println!("---->");
        println!("{:X?}", val.as_bytes());
        for i in 0..48 {
            unsafe {
                print!("{:02X} ", *pkt_data.offset(i as isize));
            }
        }
        println!("");*/
    }

    fn construct_set_request_vec(&mut self) -> Vec<u8> {
        let mut v: Vec<u8> = Vec::with_capacity(SET_PAYLOAD);
        let (key, val, req_id) = self.set_one();

        let set_hdr: [u8; HEADER + COMMAND] = [0, 0, 0, 0, //request_id
                0, 0, //seq_nr
                0, 1, //datagram_total
                0, 0, //reserved
                b's', b'e', b't', b' '];

        v.extend(&set_hdr);
        v.extend(key.into_bytes());

        v[0..2].copy_from_slice(&req_id.to_be_bytes());

        /*
        let x: String = format!("[{}][{}]", self.set_cursor, self.get_cursor);
        let a = v.len() - x.len();
        v[a..].copy_from_slice(&x.into_bytes());
        */

        let set_body: [u8; FLAGS_SIZE] = [b' ', //space1,
                    0, b' ', 0, b' ',
                    0, 0, 0, 0, //len
                    b'\r', b'\n' //newline1
                    ];

        v.extend(&set_body);
        v.extend(val.into_bytes());
        v.push(b'\r');
        v.push(b'\n');
        v
    }

    fn construct_get_request(&mut self, pkt_data: *mut u8) {
        let (key, val) = self.get_one_rand();

        let get_hdr: [u8; HEADER + COMMAND] = [0, 0, 0, 0, //request_id
                0, 0, //seq_nr
                0, 1, //datagram_total
                0, 0, //reserved
                b'g', b'e', b't', b' '];

        let mut copy_len = get_hdr.len();

        // Copy header
        unsafe {
            std::ptr::copy(get_hdr.as_ptr(), pkt_data, copy_len);
        }

        let mut offset = copy_len;
        copy_len = key.as_bytes().len();

        // Copy key to get
        unsafe {
            std::ptr::copy(key.as_bytes().as_ptr(), pkt_data.offset(offset as isize), copy_len);
        }

        let fin = [b'\r', b'\n'];

        offset += copy_len;
        copy_len = fin.len();

        // Copy end byte
        unsafe {
            std::ptr::copy((&fin).as_ptr(), pkt_data.offset(offset as isize), copy_len);
        }
    }

    fn construct_get_request_vec(&mut self) -> Vec<u8> {
        let mut v: Vec<u8> = Vec::with_capacity(GET_PAYLOAD);

        let (key, val) = self.get_one();

        let get_hdr: [u8; HEADER + COMMAND] = [0, 0, 0, 0, //request_id
                0, 0, //seq_nr
                0, 1, //datagram_total
                0, 0, //reserved
                b'g', b'e', b't', b' '];

        v.extend(&get_hdr);
        v.extend(key.into_bytes());

        /*
        let x: String = format!("[{}][{}]", self.set_cursor, self.get_cursor);
        let a = v.len() - x.len();
        v[a..].copy_from_slice(&x.into_bytes());
        */

        v.push(b'\r');
        v.push(b'\n');
        v
    }
}

pub fn main() {
    simple_logger::init().unwrap();

    let mut kv = KVStore::new_with_capacity(KEYS_TOTAL);

    kv.populate();

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

    let mut buffer: VecDeque<Packet> = VecDeque::with_capacity(BATCH_SIZE);
   
    /*
     * 90 E2 BA B3 74 81 90 E2 BA B5 14 CD 08 00 45 00
     * 00 2E 00 00 00 00 40 11 60 A9 0A 0A 03 01 0A 0A
     * 03 02 B2 6F 14 51 00 1A 9C AF 52 65 64 6C 65 61
     * 67 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    */
    #[rustfmt::skip]
    let set_pkt_data = [
        //  90:e2:ba:b5:02:01
        0x90, 0xe2, 0xba, 0xb5, 0x02, 0x01, // Dst mac
        // 90:e2:ba:b5:14:59
        0x90, 0xe2, 0xba, 0xb5, 0x14, 0x59, // Src mac
        0x08, 0x00,                                 // ether type: IPv4
        0x45, 0x00,                                 // Version, IHL, TOS
        ((SET_PACKET_SIZE - 14) >> 8) as u8,            // ip len excluding ethernet, high byte
        ((SET_PACKET_SIZE - 14) & 0xFF) as u8,          // ip len excluding ethernet, low byte
        0x00, 0x00, 0x00, 0x00,                     // id, flags, fragmentation
        0x40, 0x11, 0x00, 0x00,                     // TTL (64), protocol (UDP), checksum
        0x0A, 0x0A, 0x03, 0x02,                     // src ip (10.10.3.2)
        0x0A, 0x0A, 0x03, 0x01,                     // dst ip (10.10.3.1)
        0x00, 0x2A, 0x14, 0x51,                     // src and dst ports (42 -> 5201)
        ((SET_PACKET_SIZE - 20 - 14) >> 8) as u8,       // udp len excluding ip & ethernet, high byte
        ((SET_PACKET_SIZE - 20 - 14) & 0xFF) as u8,     // udp len excluding ip & ethernet, low byte
        0x00, 0x00,                                 // udp checksum, optional
    ];

    let pool = Mempool::allocate(NUM_PACKETS, 0).unwrap();

    // pre-fill all packet buffer in the pool with data and return them to the packet pool
    {
        let mut buffer: VecDeque<Packet> = VecDeque::with_capacity(NUM_PACKETS);

        alloc_pkt_batch(&pool, &mut buffer, NUM_PACKETS, SET_PACKET_SIZE);

        for p in buffer.iter_mut() {
            let mut pkt_data: Vec<u8> = set_pkt_data.to_vec();
            for (i, data) in pkt_data.iter().enumerate() {
                p[i] = *data;
            }
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
    //let mut time = Instant::now();
    let mut time = get_rdtsc();
    let mut seq_num = 0;
    let mut counter = 0;

    for (i, b) in set_pkt_data.iter().enumerate() {
        print!("{:02X} ", b);

        if i > 0 && (i + 1) % 16 == 0 {
            println!("");
        }
    }
    println!("");

    let mut pkt_counter: usize = 0;
    loop {
        alloc_pkt_batch(&pool, &mut buffer, BATCH_SIZE, SET_PACKET_SIZE);

        // update sequence number of all packets (and checksum if necessary)
        for p in buffer.iter_mut() {
            let mut pkt_size = SET_PACKET_SIZE;
            let mut dst_pkt_ptr = unsafe { p.as_mut_ptr().offset(set_pkt_data.len() as isize) };
            let mut pkt_ptr = unsafe { p.as_mut_ptr() };

            pkt_counter = pkt_counter.wrapping_add(1);

            if kv.set_complete != true {
                kv.construct_set_request(dst_pkt_ptr);
                p[14 + 2] = ((SET_PACKET_SIZE - 14) >> 8) as u8;            // ip len excluding ethernet, high byte
                p[14 + 2 + 1] = ((SET_PACKET_SIZE - 14) & 0xFF) as u8;          // ip len excluding ethernet, low byte
            } else {
                // Send set requests for 100 batches
                if pkt_counter < KEYS_SET {
                    // | mempunch header                             | mempunch data                                       |
                    // | <req_id:2> <seq_nr:2> <pkt_len:2> <rsrvd:2> | SET <key:KEY_SIZE> <flags:4> \n <val:VALUE_SIZE> \n |
                    kv.construct_set_request(dst_pkt_ptr);
                    p[14 + 2] = ((SET_PACKET_SIZE - 14) >> 8) as u8;            // ip len excluding ethernet, high byte
                    p[14 + 2 + 1] = ((SET_PACKET_SIZE - 14) & 0xFF) as u8;          // ip len excluding ethernet, low byte
                } else if pkt_counter >= KEYS_SET && pkt_counter < KEYS_TOTAL {
                    kv.construct_get_request(dst_pkt_ptr);
                    p[14 + 2] = ((GET_PACKET_SIZE - 14) >> 8) as u8;            // ip len excluding ethernet, high byte
                    p[14 + 2 + 1] = ((GET_PACKET_SIZE - 14) & 0xFF) as u8;          // ip len excluding ethernet, low byte
                } else {
                    // reset pkt_counter
                    pkt_counter = 0;
                }
            }
            //let checksum = calc_ipv4_checksum(&p[14..14 + 20]);
            // Calculated checksum is little-endian; checksum field is big-endian
            //p[24] = (checksum >> 8) as u8;
            //p[25] = (checksum & 0xff) as u8;
        }

        dev.tx_batch_busy_wait(0, &mut buffer);
        // check for rx
        {
            let num_rx = dev.rx_batch(0, &mut rx_buffer, BATCH_SIZE);
            if num_rx != 0 {
                // println!("{} packets received", num_rx);
            }
            for p in rx_buffer.iter_mut() {
                // TODO: Check sequence number to identify which
                // set request was stored and which get request was successful
               //println!("RX: {:?}", &p);
                if &p[(PAYLOAD_OFFSET + HEADER)..(PAYLOAD_OFFSET + HEADER + 6)] == b"STORED" {
                    use core::convert::TryInto;
                    let req_id: &[u8; 4] = (&p[PAYLOAD_OFFSET..(PAYLOAD_OFFSET + 4)]).try_into().unwrap();
                    let new_cursor = u32::from_be_bytes(*req_id) as usize;
                    let pkt_len: &[u8; 2] = (&p[(14 + 2)..(14 + 4)]).try_into().unwrap();
                    //println!("Rx packet size {}", u16::from_be_bytes(*pkt_len));
                    //println!("RX: {:?}", &p);
                    //println!("STORED resp ack_cursor {} new_cursor {}", kv.set_ack_cursor, new_cursor);
                    if kv.set_ack_cursor <= new_cursor {
                        kv.set_ack_cursor = new_cursor + 1;
                        if kv.set_ack_cursor == KEYS_TOTAL {
                            kv.set_complete = true;
                            println!("Set complete!");
                        }
                        //println!("Cursor -> {}", new_cursor);
                    }
                } else if &p[(PAYLOAD_OFFSET + HEADER)..(PAYLOAD_OFFSET + HEADER + 5)] == b"VALUE" {
                    /*println!("key {} value {}",
                                String::from_utf8(p[(PAYLOAD_OFFSET + HEADER + 6)..(PAYLOAD_OFFSET + HEADER + 6 + KEY_SIZE)].to_vec()).unwrap(),
                                String::from_utf8(p[(PAYLOAD_OFFSET + HEADER + 6 + KEY_SIZE + FLAGS_SIZE)..
                                        (PAYLOAD_OFFSET + HEADER + 6 + KEY_SIZE + FLAGS_SIZE + VALUE_SIZE)].to_vec()).unwrap(),
                    );*/
                }
            }

            rx_buffer.drain(..);
        }
        // don't poll the time unnecessarily
        if counter & 0xfff == 0 {
            //let elapsed = time.elapsed();
            //let nanos = elapsed.as_secs() * 1_000_000_000 + u64::from(elapsed.subsec_nanos());
            let nanos = ((get_rdtsc() - time) / 2593) * 1000;
            // every second
            if nanos > 1_000_000_000 {
                dev.read_stats(&mut dev_stats);
                dev_stats.print_stats_diff(&*dev, &dev_stats_old, nanos);
                dev_stats_old = dev_stats;

                //time = Instant::now();
                time = get_rdtsc();
            }
        }

        counter += 1;
        //break;
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
/*
fn receive(
    buffer: &mut VecDeque<Packet>,
    rx_dev: &mut dyn IxyDevice,
    rx_queue: u32,
) {
    let num_rx = rx_dev.rx_batch(rx_queue, buffer, BATCH_SIZE);

    if num_rx > 0 {
        for p in buffer.iter_mut() {
            //println!("{:x}", p[23]);
            // Check if UDP
            if p[23] == 0x11 {
                let payload = String::from_utf8_lossy(&p[42..42 + 7]);
                //println!("Udp packet {}", payload);
                if payload == String::from("Redleaf") {
                    let array = <[u8; 2]>::try_from(&p[49..49+2]).expect("need two bytes");
                    let req_pkt_size = u16::from_be_bytes(array);
                    //println!("requested size {}", req_pkt_size);
                    match  req_pkt_size {
                        64 => { send_packet(rx_dev, 256); return; },
                        128 => { send_packet(rx_dev, 128); return; },
                        256 => { send_packet(rx_dev, 256); return; },
                        512 => { send_packet(rx_dev, 512); return; },
                        1512 => { send_packet(rx_dev, 1512); return; },
                        _ => continue,
                    }
                }
            }
        }

        // drop packets if they haven't been sent out
        buffer.drain(..);
    }
}*/

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
