#include <getopt.h>
#include <rte_cycles.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_ether.h>
#include <rte_ip.h>
#include <rte_lcore.h>
#include <rte_mbuf.h>
#include <rte_udp.h>
#include <signal.h>
#include <stdbool.h>
#include <stdlib.h>
#include <unistd.h>

#define NUM_MBUFS_PER_CORE 8191
#define MBUF_CACHE_SIZE 250
#define BURST_SIZE 64
#define RX_RING_SIZE 1024
#define TX_RING_SIZE 1024

static const struct rte_eth_conf port_conf_default = {
    .rxmode = {.max_lro_pkt_size = RTE_ETHER_MAX_LEN}};

/* Global termination flag */
static volatile bool force_quit = false;

/* Global parameters */
static uint32_t g_ip_mask = 0xFF;
static uint16_t g_payload_size = 0;
static uint64_t g_timer_hz = 0;

enum core_type { CORE_TX, CORE_RX };

/* Custom payload struct for tracking */
struct custom_payload {
  uint32_t thread_id;
  uint64_t packet_id;
  uint64_t tx_timestamp;
} __attribute__((packed));

/* Structure containing configuration for each worker lcore */
struct lcore_conf {
  enum core_type type;
  uint16_t port_id;
  uint16_t queue_id;
  struct rte_mempool *mbuf_pool;
  struct rte_ether_addr port_mac;
} __rte_cache_aligned;

static struct lcore_conf lcore_config[RTE_MAX_LCORE];

/* Cache-aligned stats array to prevent False Sharing between cores */
struct worker_stats {
  uint64_t tx_pkts;
  uint64_t tx_bytes;
  uint64_t rx_pkts;
  uint64_t rx_bytes;
  uint64_t total_latency_cycles;
} __rte_cache_aligned;

static struct worker_stats g_stats[RTE_MAX_LCORE];

/* Signal handler function to intercept Ctrl+C / SIGTERM */
static void signal_handler(int signum) {
  if (signum == SIGINT || signum == SIGTERM) {
    printf("\n\nSignal %d received, preparing for safe termination...\n",
           signum);
    force_quit = true;
  }
}

static int tx_worker_loop(__rte_unused void *arg) {
  unsigned int lcore_id = rte_lcore_id();
  struct lcore_conf *conf = &lcore_config[lcore_id];

  uint16_t portid = conf->port_id;
  uint16_t queueid = conf->queue_id;
  struct rte_mempool *mbuf_pool = conf->mbuf_pool;

  uint32_t base_ip = RTE_IPV4(10, lcore_id, 0, 0);
  uint32_t client_ip_counter = 1;
  uint16_t client_port_counter = 1025 + (lcore_id * 100);

  uint32_t l2_payload_len = sizeof(struct rte_ether_hdr) +
                            sizeof(struct rte_ipv4_hdr) +
                            sizeof(struct rte_udp_hdr) + g_payload_size;

  uint32_t wire_len = l2_payload_len + 24;

  printf("Core %u spinning up [TX Worker]: Port %u, TX Queue %u\n", lcore_id,
         portid, queueid);

  uint64_t local_pkts = 0;
  uint64_t local_bytes = 0;

  uint64_t update_interval = g_timer_hz / 10;
  uint64_t last_tsc = rte_rdtsc();

  while (!force_quit) {
    struct rte_mbuf *bufs[BURST_SIZE];

    if (unlikely(rte_pktmbuf_alloc_bulk(mbuf_pool, bufs, BURST_SIZE) != 0)) {
      continue;
    }

    for (int i = 0; i < BURST_SIZE; i++) {
      struct rte_mbuf *m = bufs[i];
      char *data = rte_pktmbuf_append(m, l2_payload_len);

      struct rte_ether_hdr *eth = (struct rte_ether_hdr *)data;
      struct rte_ipv4_hdr *ip = (struct rte_ipv4_hdr *)(eth + 1);
      struct rte_udp_hdr *udp = (struct rte_udp_hdr *)(ip + 1);
      struct custom_payload *payload = (struct custom_payload *)(udp + 1);

      memset(eth, 0,
             sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr) +
                 sizeof(struct rte_udp_hdr));

      /* Layer 2 */
      memset(&eth->dst_addr, 0xFF, 6);
      rte_ether_addr_copy(&conf->port_mac, &eth->src_addr);
      eth->ether_type = rte_cpu_to_be_16(RTE_ETHER_TYPE_IPV4);

      /* Layer 3 */
      uint32_t current_ip = base_ip + (client_ip_counter & g_ip_mask);
      client_ip_counter++;

      ip->version_ihl = 0x45;
      ip->total_length =
          rte_cpu_to_be_16(sizeof(struct rte_ipv4_hdr) +
                           sizeof(struct rte_udp_hdr) + g_payload_size);
      ip->next_proto_id = IPPROTO_UDP;
      ip->src_addr = rte_cpu_to_be_32(current_ip);
      ip->dst_addr = rte_cpu_to_be_32(RTE_IPV4(192, 168, 1, 1));
      ip->time_to_live = 64;
      ip->hdr_checksum = rte_ipv4_cksum(ip);

      /* Layer 4 */
      udp->src_port = rte_cpu_to_be_16(client_port_counter);
      udp->dst_port = rte_cpu_to_be_16(80);
      udp->dgram_len =
          rte_cpu_to_be_16(sizeof(struct rte_udp_hdr) + g_payload_size);
      udp->dgram_cksum = 0;

      if (client_port_counter > 65000)
        client_port_counter = 1025;

      /* Custom Payload: Insert Metadata and Timestamp immediately before
       * sending */
      payload->thread_id = lcore_id;
      payload->packet_id = local_pkts + i;
      payload->tx_timestamp = rte_rdtsc();
    }

    uint16_t nb_tx = rte_eth_tx_burst(portid, queueid, bufs, BURST_SIZE);

    if (unlikely(nb_tx < BURST_SIZE)) {
      for (uint16_t buf = nb_tx; buf < BURST_SIZE; buf++) {
        rte_pktmbuf_free(bufs[buf]);
      }
    }

    if (nb_tx > 0) {
      local_pkts += nb_tx;
      local_bytes += (nb_tx * wire_len);
    }

    uint64_t cur_tsc = rte_rdtsc();
    if (unlikely(cur_tsc - last_tsc >= update_interval)) {
      g_stats[lcore_id].tx_pkts = local_pkts;
      g_stats[lcore_id].tx_bytes = local_bytes;
      last_tsc = cur_tsc;
    }
  }

  g_stats[lcore_id].tx_pkts = local_pkts;
  g_stats[lcore_id].tx_bytes = local_bytes;
  printf("Core %u spinning down cleanly.\n", lcore_id);
  return 0;
}

static int rx_worker_loop(__rte_unused void *arg) {
  unsigned int lcore_id = rte_lcore_id();
  struct lcore_conf *conf = &lcore_config[lcore_id];

  uint16_t portid = conf->port_id;
  uint16_t queueid = conf->queue_id;

  printf("Core %u spinning up [RX Worker]: Port %u, RX Queue %u\n", lcore_id,
         portid, queueid);

  uint64_t local_pkts = 0;
  uint64_t local_bytes = 0;
  uint64_t local_latency_cycles = 0;

  uint64_t update_interval = g_timer_hz / 10;
  uint64_t last_tsc = rte_rdtsc();

  while (!force_quit) {
    struct rte_mbuf *bufs[BURST_SIZE];
    uint16_t nb_rx = rte_eth_rx_burst(portid, queueid, bufs, BURST_SIZE);

    if (nb_rx == 0) {
      rte_pause();
      continue;
    }

    uint64_t rx_tsc = rte_rdtsc(); /* Time of reception for burst */

    for (int i = 0; i < nb_rx; i++) {
      struct rte_mbuf *m = bufs[i];

      /* Add 24 bytes wire overhead for line rate calculation */
      local_bytes += (m->pkt_len + 24);

      struct rte_ether_hdr *eth = rte_pktmbuf_mtod(m, struct rte_ether_hdr *);
      if (eth->ether_type == rte_cpu_to_be_16(RTE_ETHER_TYPE_IPV4)) {
        struct rte_ipv4_hdr *ip = (struct rte_ipv4_hdr *)(eth + 1);

        if (ip->next_proto_id == IPPROTO_UDP) {
          struct rte_udp_hdr *udp = (struct rte_udp_hdr *)(ip + 1);
          struct custom_payload *payload = (struct custom_payload *)(udp + 1);

          /* Calculate cycle difference based on embedded TX timestamp */
          if (rx_tsc > payload->tx_timestamp) {
            local_latency_cycles += (rx_tsc - payload->tx_timestamp);
          }
        }
      }
      rte_pktmbuf_free(m);
    }

    local_pkts += nb_rx;

    uint64_t cur_tsc = rte_rdtsc();
    if (unlikely(cur_tsc - last_tsc >= update_interval)) {
      g_stats[lcore_id].rx_pkts = local_pkts;
      g_stats[lcore_id].rx_bytes = local_bytes;
      g_stats[lcore_id].total_latency_cycles = local_latency_cycles;
      last_tsc = cur_tsc;
    }
  }

  g_stats[lcore_id].rx_pkts = local_pkts;
  g_stats[lcore_id].rx_bytes = local_bytes;
  g_stats[lcore_id].total_latency_cycles = local_latency_cycles;
  return 0;
}

/* Master launcher that delegates based on assigned role */
static int worker_launcher(void *arg) {
  unsigned int lcore_id = rte_lcore_id();
  if (lcore_config[lcore_id].type == CORE_TX) {
    return tx_worker_loop(arg);
  } else {
    return rx_worker_loop(arg);
  }
}

int main(int argc, char *argv[]) {
  int ret = rte_eal_init(argc, argv);
  if (ret < 0)
    rte_exit(EXIT_FAILURE, "Error with EAL initialization\n");

  argc -= ret;
  argv += ret;

  g_timer_hz = rte_get_timer_hz();

  int opt;
  while ((opt = getopt(argc, argv, "r:p:")) != -1) {
    if (opt == 'r') {
      int range = atoi(optarg);
      if (range <= 0 || (range & (range - 1)) != 0) {
        rte_exit(EXIT_FAILURE, "Invalid range: -r must be a power of 2\n");
      }
      g_ip_mask = range - 1;
    } else if (opt == 'p') {
      int payload = atoi(optarg);
      if (payload < 0 || payload > 1400) {
        rte_exit(EXIT_FAILURE,
                 "Invalid payload size: -p must be between 0 and 1400\n");
      }
      g_payload_size = (uint16_t)payload;
    }
  }

  /* Enforce minimum payload size for our custom struct */
  size_t min_payload = sizeof(struct custom_payload);
  if (g_payload_size < min_payload) {
    printf("Note: Increasing payload size to %zu bytes to fit custom timestamp "
           "header.\n",
           min_payload);
    g_payload_size = min_payload;
  }

  force_quit = false;
  signal(SIGINT, signal_handler);
  signal(SIGTERM, signal_handler);

  memset(g_stats, 0, sizeof(g_stats));

  uint16_t nb_ports = rte_eth_dev_count_avail();
  if (nb_ports == 0)
    rte_exit(EXIT_FAILURE, "No Ethernet ports detected\n");

  uint32_t nb_workers = rte_lcore_count() - 1;
  if (nb_workers < 2)
    rte_exit(EXIT_FAILURE,
             "Error: Require at least 2 worker cores (1 TX, 1 RX)\n");

  /* 1. Allocate Mempools & Assign Roles */
  uint16_t tx_queues_per_port[RTE_MAX_ETHPORTS] = {0};
  uint16_t rx_queues_per_port[RTE_MAX_ETHPORTS] = {0};
  unsigned int lcore_id;
  unsigned int worker_idx = 0;
  uint16_t port_rr = 0;

  RTE_LCORE_FOREACH_WORKER(lcore_id) {
    char pool_name[32];
    snprintf(pool_name, sizeof(pool_name), "MBUF_POOL_C%u", lcore_id);

    struct rte_mempool *mp = rte_pktmbuf_pool_create(
        pool_name, NUM_MBUFS_PER_CORE, MBUF_CACHE_SIZE, 0,
        RTE_MBUF_DEFAULT_BUF_SIZE, rte_lcore_to_socket_id(lcore_id));

    if (mp == NULL)
      rte_exit(EXIT_FAILURE, "Cannot init mbuf pool for core %u\n", lcore_id);

    lcore_config[lcore_id].mbuf_pool = mp;
    lcore_config[lcore_id].port_id = port_rr;

    /* Split workers in half: First half TX, Second half RX */
    if (worker_idx < nb_workers / 2) {
      lcore_config[lcore_id].type = CORE_TX;
      lcore_config[lcore_id].queue_id = tx_queues_per_port[port_rr]++;
    } else {
      lcore_config[lcore_id].type = CORE_RX;
      lcore_config[lcore_id].queue_id = rx_queues_per_port[port_rr]++;
    }

    rte_eth_macaddr_get(port_rr, &lcore_config[lcore_id].port_mac);
    port_rr = (port_rr + 1) % nb_ports;
    worker_idx++;
  }

  /* 2. Configure Ports & Setup Queues */
  uint16_t portid;
  RTE_ETH_FOREACH_DEV(portid) {
    uint16_t n_tx = tx_queues_per_port[portid];
    uint16_t n_rx = rx_queues_per_port[portid];

    if (n_tx == 0 && n_rx == 0)
      continue;

    printf("Configuring Port %u (RX Queues: %u, TX Queues: %u)...\n", portid,
           n_rx, n_tx);
    if (rte_eth_dev_configure(portid, n_rx, n_tx, &port_conf_default) < 0) {
      rte_exit(EXIT_FAILURE, "Cannot configure port %u\n", portid);
    }

    /* Setup RX Queues mapped to their specific core mempools */
    RTE_LCORE_FOREACH_WORKER(lcore_id) {
      if (lcore_config[lcore_id].port_id == portid &&
          lcore_config[lcore_id].type == CORE_RX) {
        if (rte_eth_rx_queue_setup(portid, lcore_config[lcore_id].queue_id,
                                   RX_RING_SIZE, rte_eth_dev_socket_id(portid),
                                   NULL,
                                   lcore_config[lcore_id].mbuf_pool) < 0) {
          rte_exit(EXIT_FAILURE, "RX queue setup failed\n");
        }
      }
    }

    /* Setup TX Queues */
    RTE_LCORE_FOREACH_WORKER(lcore_id) {
      if (lcore_config[lcore_id].port_id == portid &&
          lcore_config[lcore_id].type == CORE_TX) {
        if (rte_eth_tx_queue_setup(portid, lcore_config[lcore_id].queue_id,
                                   TX_RING_SIZE, rte_eth_dev_socket_id(portid),
                                   NULL) < 0) {
          rte_exit(EXIT_FAILURE, "TX queue setup failed\n");
        }
      }
    }

    /* Enable promiscuous mode so loopback traffic doesn't drop due to MAC
     * mismatches */
    rte_eth_promiscuous_enable(portid);

    if (rte_eth_dev_start(portid) < 0) {
      rte_exit(EXIT_FAILURE, "Cannot start port %u\n", portid);
    }
  }

  printf("Waiting 4 seconds for physical links to come up...\n");
  sleep(4);

  printf("Master core finished mapping. Launching workers...\n");
  rte_eal_mp_remote_launch(worker_launcher, NULL, SKIP_MAIN);

  /* --- Main Core Stats Aggregation Loop --- */
  uint64_t prev_tx_pkts[RTE_MAX_LCORE] = {0};
  uint64_t prev_tx_bytes[RTE_MAX_LCORE] = {0};
  uint64_t prev_rx_pkts[RTE_MAX_LCORE] = {0};
  uint64_t prev_rx_bytes[RTE_MAX_LCORE] = {0};
  uint64_t prev_lat_cycles[RTE_MAX_LCORE] = {0};

  /* Add an array to track previous hardware stats for delta calculations */
  struct rte_eth_stats prev_hw_stats[RTE_MAX_ETHPORTS];
  memset(prev_hw_stats, 0, sizeof(prev_hw_stats));

  const char clr[] = {27, '[', '2', 'J', '\0'};
  while (!force_quit) {
    sleep(1);
    if (force_quit)
      break;

    uint64_t tot_tx_pkts_sec = 0, tot_tx_bytes_sec = 0;
    uint64_t tot_rx_pkts_sec = 0, tot_rx_bytes_sec = 0;
    uint64_t tot_lat_cycles_sec = 0;

    /* Aggregate Software Worker Stats */
    RTE_LCORE_FOREACH_WORKER(lcore_id) {
      uint64_t c_tx_pkts = g_stats[lcore_id].tx_pkts;
      uint64_t c_tx_bytes = g_stats[lcore_id].tx_bytes;
      uint64_t c_rx_pkts = g_stats[lcore_id].rx_pkts;
      uint64_t c_rx_bytes = g_stats[lcore_id].rx_bytes;
      uint64_t c_lat_cyc = g_stats[lcore_id].total_latency_cycles;

      tot_tx_pkts_sec += (c_tx_pkts - prev_tx_pkts[lcore_id]);
      tot_tx_bytes_sec += (c_tx_bytes - prev_tx_bytes[lcore_id]);
      tot_rx_pkts_sec += (c_rx_pkts - prev_rx_pkts[lcore_id]);
      tot_rx_bytes_sec += (c_rx_bytes - prev_rx_bytes[lcore_id]);
      tot_lat_cycles_sec += (c_lat_cyc - prev_lat_cycles[lcore_id]);

      prev_tx_pkts[lcore_id] = c_tx_pkts;
      prev_tx_bytes[lcore_id] = c_tx_bytes;
      prev_rx_pkts[lcore_id] = c_rx_pkts;
      prev_rx_bytes[lcore_id] = c_rx_bytes;
      prev_lat_cycles[lcore_id] = c_lat_cyc;
    }

    /* Aggregate Hardware Port Stats */
    uint64_t tot_imissed_sec = 0;
    uint64_t tot_ierrors_sec = 0;
    uint64_t tot_rx_nombuf_sec = 0;
    uint64_t tot_oerrors_sec = 0;

    RTE_ETH_FOREACH_DEV(portid) {
      /* Skip unconfigured ports */
      if (tx_queues_per_port[portid] == 0 && rx_queues_per_port[portid] == 0)
        continue;

      struct rte_eth_stats hw_stats;
      if (rte_eth_stats_get(portid, &hw_stats) == 0) {
        tot_imissed_sec += (hw_stats.imissed - prev_hw_stats[portid].imissed);
        tot_ierrors_sec += (hw_stats.ierrors - prev_hw_stats[portid].ierrors);
        tot_rx_nombuf_sec +=
            (hw_stats.rx_nombuf - prev_hw_stats[portid].rx_nombuf);
        tot_oerrors_sec += (hw_stats.oerrors - prev_hw_stats[portid].oerrors);

        /* Update previous stats for the next second */
        prev_hw_stats[portid] = hw_stats;
      }
    }

    double tx_mpps = (double)tot_tx_pkts_sec / 1000000.0;
    double tx_gbps = ((double)tot_tx_bytes_sec * 8.0) / 1000000000.0;

    double rx_mpps = (double)tot_rx_pkts_sec / 1000000.0;
    double rx_gbps = ((double)tot_rx_bytes_sec * 8.0) / 1000000000.0;

    double avg_lat_ns = 0.0;
    if (tot_rx_pkts_sec > 0) {
      double avg_cycles = (double)tot_lat_cycles_sec / (double)tot_rx_pkts_sec;
      avg_lat_ns = (avg_cycles * 1000000000.0) / (double)g_timer_hz;
    }

    printf("%s======================================================\n", clr);
    printf(" TX: %7.2f Mpps | %7.2f Gbps\n", tx_mpps, tx_gbps);
    printf(" RX: %7.2f Mpps | %7.2f Gbps\n", rx_mpps, rx_gbps);
    printf(" Latency: %.2f ns\n", avg_lat_ns);
    printf("------------------------------------------------------\n");
    printf(" HW Drops/sec : %" PRIu64 " missed (NIC queue full)\n",
           tot_imissed_sec);
    printf(" HW No-Mbufs  : %" PRIu64 " (SW pool empty)\n", tot_rx_nombuf_sec);
    printf(" HW Errors/sec: %" PRIu64 " RX | %" PRIu64 " TX\n", tot_ierrors_sec,
           tot_oerrors_sec);
    printf("======================================================\n");
  }

  rte_eal_mp_wait_lcore();

  printf("Stopping and closing network ports...\n");
  RTE_ETH_FOREACH_DEV(portid) {
    if (tx_queues_per_port[portid] == 0 && rx_queues_per_port[portid] == 0)
      continue;
    printf("Closing Port %u...\n", portid);
    rte_eth_dev_stop(portid);
    rte_eth_dev_close(portid);
  }

  printf("DPDK clean exit completed successfully.\n");
  return 0;
}
