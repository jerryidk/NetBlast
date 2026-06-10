/* SPDX-License-Identifier: BSD-3-Clause */
#include <ctype.h>
#include <errno.h>
#include <getopt.h>
#include <inttypes.h>
#include <netinet/in.h>
#include <setjmp.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/queue.h>
#include <sys/types.h>

#include <rte_atomic.h>
#include <rte_branch_prediction.h>
#include <rte_common.h>
#include <rte_cycles.h>
#include <rte_debug.h>
#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_ether.h>
#include <rte_interrupts.h>
#include <rte_launch.h>
#include <rte_lcore.h>
#include <rte_log.h>
#include <rte_malloc.h>
#include <rte_mbuf.h>
#include <rte_memcpy.h>
#include <rte_memory.h>
#include <rte_mempool.h>
#include <rte_per_lcore.h>
#include <rte_prefetch.h>
#include <rte_random.h>
#include <time.h>
#include <unistd.h>

#include "dramblast.h"
#include "generic/rte_cycles.h"
#include "generic/rte_pause.h"
#include "maglev.h"
#include "packettool.h"
#include "sashstore.h"

#define RTE_LOGTYPE_L2FWD RTE_LOGTYPE_USER1

#define MAX_PKT_BURST 64
#define BURST_TX_DRAIN_US 100
#define MEMPOOL_CACHE_SIZE 256

#define RTE_TEST_RX_DESC_DEFAULT 4096
#define RTE_TEST_TX_DESC_DEFAULT 4096
static uint16_t nb_rxd = RTE_TEST_RX_DESC_DEFAULT;
static uint16_t nb_txd = RTE_TEST_TX_DESC_DEFAULT;

uint64_t CAPACITY = (1 << 20) * 16;
static volatile bool force_quit;

static int mac_updating = 1; /* Enabled by default per your usage string */

static uint32_t l2fwd_enabled_port_mask = 0;
static uint32_t l2fwd_dst_ports[RTE_MAX_ETHPORTS];
static struct rte_ether_addr l2fwd_ports_eth_addr[RTE_MAX_ETHPORTS];

static uint32_t l2fwd_dramblast_enabled = 0;
static uint32_t l2fwd_maglev_enabled = 0;
static uint32_t l2fwd_sashstore_enabled = 0;

static unsigned int l2fwd_queues_per_port = 1;

#define MAX_RX_QUEUE_PER_LCORE 16

/* Lcore configuration */
struct lcore_queue_conf {
  unsigned n_rx_port;
  struct {
    unsigned port_id;
    unsigned queue_id; /* Same ID used for RX and TX to avoid locks */
  } rx_port_list[MAX_RX_QUEUE_PER_LCORE];
  struct rte_eth_dev_tx_buffer *tx_buffer[RTE_MAX_ETHPORTS];
} __rte_cache_aligned;

struct lcore_queue_conf lcore_queue_conf[RTE_MAX_LCORE];

/* Enable RSS for multithreading on the same port */
static struct rte_eth_conf port_conf = {
    .rxmode =
        {
            .mq_mode = RTE_ETH_MQ_RX_RSS,
        },
    .rx_adv_conf =
        {
            .rss_conf =
                {
                    .rss_key = NULL,
                    .rss_hf =
                        RTE_ETH_RSS_IP | RTE_ETH_RSS_UDP | RTE_ETH_RSS_TCP,
                },
        },
    .txmode =
        {
            .mq_mode = RTE_ETH_MQ_TX_NONE,
        },
};

struct rte_mempool *l2fwd_pktmbuf_pool = NULL;

/* Per-port, Per-lcore statistics struct to prevent false sharing */
struct l2fwd_port_statistics {
  uint64_t tx;
  uint64_t rx;
  uint64_t rx_cnt;
  uint64_t fwded;
  uint64_t dropped;
  uint64_t rx_dropped;
  uint64_t tx_dropped;
  uint64_t hash_tsc;
} __rte_cache_aligned;

struct l2fwd_port_statistics port_statistics[RTE_MAX_ETHPORTS][RTE_MAX_LCORE];

#define MAX_TIMER_PERIOD 86400
static uint64_t timer_period = 1; // update 1 sec

/* Helper to aggregate stats across all lcores for a specific port */
static void get_aggregated_stats(unsigned portid,
                                 struct l2fwd_port_statistics *agg) {
  memset(agg, 0, sizeof(*agg));
  unsigned lcore_id;
  for (lcore_id = 0; lcore_id < RTE_MAX_LCORE; lcore_id++) {
    agg->tx += port_statistics[portid][lcore_id].tx;
    agg->rx += port_statistics[portid][lcore_id].rx;
    agg->dropped += port_statistics[portid][lcore_id].dropped;
    agg->hash_tsc += port_statistics[portid][lcore_id].hash_tsc;
    agg->fwded += port_statistics[portid][lcore_id].fwded;
    agg->tx_dropped += port_statistics[portid][lcore_id].tx_dropped;
    agg->rx_cnt += port_statistics[portid][lcore_id].rx_cnt;
  }
}

void print_port_stats(uint16_t portid) {
  struct rte_eth_stats stats;

  // Fetch hardware stats from the NIC
  if (rte_eth_stats_get(portid, &stats) == 0) {
    printf("\n--- Port %u Hardware Stats ---\n", portid);
    printf("RX-Packets (Good)  : %" PRIu64 "\n", stats.ipackets);
    printf("RX-Missed (Dropped): %" PRIu64 " (Hardware queue full)\n",
           stats.imissed);
    printf("RX-Errors (Bad)    : %" PRIu64 " (MAC mismatch, bad CRC, etc.)\n",
           stats.ierrors);
    printf("RX-No-Mbuf         : %" PRIu64 " (Software ran out of memory)\n",
           stats.rx_nombuf);
    printf("TX-Packets (Good)  : %" PRIu64 "\n", stats.opackets);
    printf("TX-Errors          : %" PRIu64 "\n", stats.oerrors);
    printf("------------------------------\n");
  } else {
    printf("Failed to get stats for Port %u\n", portid);
  }
}

uint32_t SAMPLE_SIZE = 0;
uint32_t *samples;
#define TOTAL_SAMPLES 32

uint64_t total_packets_fwded_prev = 0;

struct l2fwd_port_statistics prev_agg;
static void print_stats(void) {
  uint64_t total_packets_fwded = 0, total_hash_duration = 0;
  unsigned portid;
  struct l2fwd_port_statistics agg;

  const char clr[] = {27, '[', '2', 'J', '\0'};
  const char topLeft[] = {27, '[', '1', ';', '1', 'H', '\0'};

  printf("%s%s\nPort statistics ====================================", clr,
         topLeft);

  for (portid = 0; portid < RTE_MAX_ETHPORTS; portid++) {
    if ((l2fwd_enabled_port_mask & (1 << portid)) == 0)
      continue;

    get_aggregated_stats(portid, &agg);

    printf("\nStatistics for port %u ------------------------------"
           "\nPackets sent: %24" PRIu64 "\nPackets received: %20" PRIu64
           "\nPackets forwarded: %20" PRIu64 "\nPackets dropped: %21" PRIu64
           "\nPackets tx dropped: %21" PRIu64,
           portid, agg.tx, agg.rx, agg.fwded, agg.dropped, agg.tx_dropped);

    if((agg.rx_cnt - prev_agg.rx_cnt) > 0){
        printf("\nAverage rx batch sz %lu", (agg.rx - prev_agg.rx) / (agg.rx_cnt - prev_agg.rx_cnt));
    }
    total_packets_fwded += agg.fwded;
    total_hash_duration += agg.hash_tsc;
    print_port_stats(portid);
    prev_agg = agg;
  }

  if (timer_period > 0) {
    double t_s = timer_period / rte_get_timer_hz();
    double mpps = ((total_packets_fwded-total_packets_fwded_prev) / 1000000.0) / t_s;
    printf("\n%.2f Mpps", mpps);
    samples[SAMPLE_SIZE] = mpps;
    SAMPLE_SIZE++;
  }

  if (total_packets_fwded > 0) {
    printf("\nCycle per fwd packet: %lu",
           total_hash_duration / total_packets_fwded);
  }

  total_packets_fwded_prev = total_packets_fwded;
  printf("\n====================================================\n");
}

static void print_final_stats() {
  // Check for an empty array to avoid division by zero
  if (SAMPLE_SIZE <= 0) {
    printf("No samples to process.\n");
    return;
  }

  // Initialize min and max to the first element in the array
  uint64_t min = samples[0];
  uint64_t max = samples[0];
  double sum =
      0.0; // Use double to prevent integer overflow and get an accurate average

  // Loop through the array to find min, max, and the total sum
  for (uint32_t i = 0; i < SAMPLE_SIZE; i++) {
    if (samples[i] < min) {
      min = samples[i];
    }

    if (samples[i] > max) {
      max = samples[i];
    }

    sum += samples[i];
  }

  // Calculate the average
  double avg = sum / SAMPLE_SIZE;

  // Print the results
  printf("Minimum: %lu\n", min);
  printf("Maximum: %lu\n", max);
  printf("Average: %.2f\n", avg);
}

static inline void l2fwd_mac_updating(struct rte_mbuf *m, unsigned dest_portid,
                                      uint64_t mac) {
  struct rte_ether_hdr *eth = rte_pktmbuf_mtod(m, struct rte_ether_hdr *);
  *((uint64_t *)&eth->dst_addr.addr_bytes[0]) = mac;
  rte_ether_addr_copy(&l2fwd_ports_eth_addr[dest_portid], &eth->src_addr);
}

static inline void l2fwd_simple_forward(struct rte_mbuf *m, unsigned portid,
                                        unsigned queue_id, unsigned lcore_id) {
  unsigned dst_port = l2fwd_dst_ports[portid];
  struct rte_eth_dev_tx_buffer *buffer =
      lcore_queue_conf[lcore_id].tx_buffer[dst_port];

  if (mac_updating) {
    l2fwd_mac_updating(m, dst_port, 0xdeadbeef);
  }

  /* We transmit using the same queue_id to avoid cross-core lock contention */
  int sent = rte_eth_tx_buffer(dst_port, queue_id, buffer, m);
  if (sent)
    port_statistics[dst_port][lcore_id].tx += sent;
}

static void l2fwd_main_loop(void) {
  struct rte_mbuf *pkts_burst[MAX_PKT_BURST];
  struct rte_mbuf *m;
  uint64_t mac_addrs[MAX_PKT_BURST];
  void *frames[MAX_PKT_BURST];
  dramblast_arg_t args[MAX_PKT_BURST];

  unsigned lcore_id = rte_lcore_id();
  struct lcore_queue_conf *qconf = &lcore_queue_conf[lcore_id];

  if (qconf->n_rx_port == 0) {
    RTE_LOG(INFO, L2FWD, "lcore %u has nothing to do\n", lcore_id);
    return;
  }

  while (!force_quit) {
    for (unsigned i = 0; i < qconf->n_rx_port; i++) {
      unsigned portid = qconf->rx_port_list[i].port_id;
      unsigned queueid = qconf->rx_port_list[i].queue_id;
      unsigned nb_rx =
          rte_eth_rx_burst(portid, queueid, pkts_burst, MAX_PKT_BURST);

      port_statistics[portid][lcore_id].rx_cnt += 1;
      if (nb_rx > 0) {
        port_statistics[portid][lcore_id].rx += nb_rx;

        uint64_t start = rte_rdtsc();
        if (l2fwd_maglev_enabled) {
          for (uint16_t j = 0; j < nb_rx; j++) {
            m = pkts_burst[j];
            uint64_t mac = maglev_process_frame(rte_pktmbuf_mtod(m, void *));
            if (mac == 0) {
              port_statistics[portid][lcore_id].dropped += 1;
            } else {
              l2fwd_mac_updating(m, l2fwd_dst_ports[portid], mac);
              port_statistics[portid][lcore_id].fwded += 1;
            }
          }
        } else if (l2fwd_dramblast_enabled) {
          unsigned int fn = 0;
          uint64_t hash;

          for (unsigned int j = 0; j < nb_rx; j++) {
            m = pkts_burst[j];
            hash = flowhash(rte_pktmbuf_mtod(m, void *));
            if (hash > 0) {
              frames[fn] = m;
              args[fn].k = hash;
              args[fn].id = fn;
              fn++;
            }
          }

          if (fn > 0)
            dramblast_process_frames(args, fn, mac_addrs, lcore_id);

          uint64_t found = 0;
          for (unsigned int j = 0; j < fn; j++) {
            if (mac_addrs[j] > 0) {
              l2fwd_mac_updating(frames[j], l2fwd_dst_ports[portid],
                                 mac_addrs[j]);
              found++;
            }
          }

          port_statistics[portid][lcore_id].fwded += found;
          port_statistics[portid][lcore_id].dropped += (nb_rx - found);
        } else {
          for (uint16_t j = 0; j < nb_rx; j++) {
            unsigned dst_port = l2fwd_dst_ports[portid];
            uint64_t mac = 0xff;
            l2fwd_mac_updating(pkts_burst[j], dst_port, mac);
          }
          port_statistics[portid][lcore_id].fwded += nb_rx;
        }

        port_statistics[portid][lcore_id].hash_tsc += (rte_rdtsc() - start);

        uint16_t nb_tx = rte_eth_tx_burst(portid, queueid, pkts_burst, nb_rx);
        if (unlikely(nb_tx < nb_rx)) {
          for (uint16_t buf = nb_tx; buf < nb_rx; buf++) {
            rte_pktmbuf_free(pkts_burst[buf]);
          }
          port_statistics[portid][lcore_id].tx_dropped += nb_rx - nb_tx;
        }

        if (nb_tx > 0) {
          port_statistics[portid][lcore_id].tx += nb_tx;
        }

      } else {
        rte_pause();
      }
    }
  }
}

static int l2fwd_launch_one_lcore(__attribute__((unused)) void *dummy) {
  l2fwd_main_loop();
  return 0;
}

static void signal_handler(int signum) {
  if (signum == SIGINT || signum == SIGTERM) {
    printf("\n\nSignal %d received, preparing to exit...\n", signum);
    force_quit = true;
  }
}

static void l2fwd_usage(const char *prgname) {
  printf("%s [EAL options] -- -p PORTMASK [-q NQ]\n"
         "  -m MODE: mode specified as a string (maglev | sashstore | "
         "dramblast | none)\n"
         "  -p PORTMASK: hexadecimal bitmask of ports to configure\n"
         "  -q NQ: number of queues per port (multithread scaling, default 1)\n"
         "  -T PERIOD: statistics refresh period in seconds\n"
         "  -c CAPACITY: hashtable capacity\n"
         "  --[no-]mac-updating: Enable/disable MAC updating\n",
         prgname);
}

static int l2fwd_parse_args(int argc, char **argv) {
  int opt, ret;
  char **argvopt = argv;
  int option_index;
  char *prgname = argv[0];

  static const struct option lgopts[] = {
      {"mac-updating", no_argument, &mac_updating, 1},
      {"no-mac-updating", no_argument, &mac_updating, 0},
      {NULL, 0, 0, 0}};

  while ((opt = getopt_long(argc, argvopt, "p:q:T:m:c:", lgopts,
                            &option_index)) != EOF) {
    switch (opt) {
    case 0:
      break;
    case 'm':
      if (!strncmp(optarg, "maglev", 6))
        l2fwd_maglev_enabled = 1;
      else if (!strncmp(optarg, "sashstore", 9))
        l2fwd_sashstore_enabled = 1;
      else if (!strncmp(optarg, "dramblast", 9))
        l2fwd_dramblast_enabled = 1;
      else if (!strncmp(optarg, "none", 4)) {
        l2fwd_dramblast_enabled = 0;
        l2fwd_maglev_enabled = 0;
        l2fwd_sashstore_enabled = 0;
      } else
        return -1;
      break;
    case 'c':
      CAPACITY = strtoull(optarg, NULL, 10);
      if (CAPACITY == 0 || (CAPACITY & (CAPACITY - 1)) != 0) {
        fprintf(stderr,
                "Error: Capacity (-c %llu) must be a power of 2 "
                "(e.g., 1048576, 16777216).\n",
                CAPACITY);
        return -1;
      }

      double g = 1024 * 1024 * 1024.0;
      printf("hashtable size %.2f gb\n", (CAPACITY * 16) / g);
      break;
    case 'p':
      l2fwd_enabled_port_mask = strtoul(optarg, NULL, 16);
      if (l2fwd_enabled_port_mask == 0)
        return -1;
      break;
    case 'q':
      l2fwd_queues_per_port = strtoul(optarg, NULL, 10);
      if (l2fwd_queues_per_port == 0)
        return -1;
      break;
    case 'T':
      timer_period = strtol(optarg, NULL, 10);
      break;
    default:
      l2fwd_usage(prgname);
      return -1;
    }
  }

  if (optind >= 0)
    argv[optind - 1] = prgname;
  ret = optind - 1;
  optind = 1;
  return ret;
}

int main(int argc, char **argv) {
  int ret;
  uint16_t nb_ports, portid, last_port;
  unsigned lcore_id, rx_lcore_id;
  unsigned nb_ports_in_mask = 0;

  ret = rte_eal_init(argc, argv);
  if (ret < 0)
    rte_exit(EXIT_FAILURE, "Invalid EAL arguments\n");
  argc -= ret;
  argv += ret;

  force_quit = false;
  signal(SIGINT, signal_handler);
  signal(SIGTERM, signal_handler);

  if (l2fwd_parse_args(argc, argv) < 0)
    rte_exit(EXIT_FAILURE, "Invalid L2FWD arguments\n");

  timer_period *= rte_get_timer_hz();

  nb_ports = rte_eth_dev_count_avail();
  if (nb_ports == 0)
    rte_exit(EXIT_FAILURE, "No Ethernet ports\n");

  /* Map destination ports identically to original code */
  for (portid = 0; portid < RTE_MAX_ETHPORTS; portid++)
    l2fwd_dst_ports[portid] = 0;
  last_port = 0;

  RTE_ETH_FOREACH_DEV(portid) {
    if ((l2fwd_enabled_port_mask & (1 << portid)) == 0)
      continue;
    if (nb_ports_in_mask % 2) {
      l2fwd_dst_ports[portid] = last_port;
      l2fwd_dst_ports[last_port] = portid;
    } else {
      last_port = portid;
    }
    nb_ports_in_mask++;
  }
  if (nb_ports_in_mask % 2)
    l2fwd_dst_ports[last_port] = last_port;

  rx_lcore_id = 0;

  /* Distribute port-queues across available lcores */
  RTE_ETH_FOREACH_DEV(portid) {
    if ((l2fwd_enabled_port_mask & (1 << portid)) == 0)
      continue;

    for (unsigned q = 0; q < l2fwd_queues_per_port; q++) {
      /* Skip main lcore to ensure it's dedicated strictly to print logic */
      while (rte_lcore_is_enabled(rx_lcore_id) == 0 ||
             rx_lcore_id == rte_get_main_lcore() ||
             lcore_queue_conf[rx_lcore_id].n_rx_port ==
                 MAX_RX_QUEUE_PER_LCORE) {
        rx_lcore_id++;
        if (rx_lcore_id >= RTE_MAX_LCORE)
          rte_exit(EXIT_FAILURE, "Not enough cores\n");
      }

      struct lcore_queue_conf *qconf = &lcore_queue_conf[rx_lcore_id];
      qconf->rx_port_list[qconf->n_rx_port].port_id = portid;
      qconf->rx_port_list[qconf->n_rx_port].queue_id = q;
      qconf->n_rx_port++;

      printf("Lcore %u assigned to Port %u Queue %u\n", rx_lcore_id, portid, q);

      rx_lcore_id++; /* Move to next core for the next queue to spread the load */
    }
  }

  // Multiply descriptors by the number of queues per port
  unsigned nb_mbufs =
      RTE_MAX(nb_ports * ((l2fwd_queues_per_port * nb_rxd) +
                          (l2fwd_queues_per_port * nb_txd) + MAX_PKT_BURST +
                          (RTE_MAX_LCORE * MEMPOOL_CACHE_SIZE)),
              8192U);

  /* Optional: Add a safety buffer for high-speed drops */
  nb_mbufs += 8192;

  l2fwd_pktmbuf_pool =
      rte_pktmbuf_pool_create("mbuf_pool", nb_mbufs, MEMPOOL_CACHE_SIZE, 0,
                              RTE_MBUF_DEFAULT_BUF_SIZE, rte_socket_id());
  if (l2fwd_pktmbuf_pool == NULL)
    rte_exit(EXIT_FAILURE, "Cannot init mbuf pool\n");

  /* Initialize Ports */
  RTE_ETH_FOREACH_DEV(portid) {
    if ((l2fwd_enabled_port_mask & (1 << portid)) == 0)
      continue;

    struct rte_eth_dev_info dev_info;
    rte_eth_dev_info_get(portid, &dev_info);

    struct rte_eth_conf local_port_conf = port_conf;
    // if (dev_info.tx_offload_capa & DEV_TX_OFFLOAD_MBUF_FAST_FREE)
    //     local_port_conf.txmode.offloads |= DEV_TX_OFFLOAD_MBUF_FAST_FREE;

    // rss
    local_port_conf.rx_adv_conf.rss_conf.rss_hf &=
        dev_info.flow_type_rss_offloads;

    /* If it supports nothing (like the old VM), disable RSS entirely */
    if (local_port_conf.rx_adv_conf.rss_conf.rss_hf == 0) {
      local_port_conf.rxmode.mq_mode = RTE_ETH_MQ_RX_NONE;
    }

    /* Configure device with Multiple RX/TX Queues */
    ret = rte_eth_dev_configure(portid, l2fwd_queues_per_port,
                                l2fwd_queues_per_port, &local_port_conf);
    if (ret < 0)
      rte_exit(EXIT_FAILURE, "Cannot configure device: err=%d, port=%u\n", ret,
               portid);

    rte_eth_macaddr_get(portid, &l2fwd_ports_eth_addr[portid]);

    /* Setup RX Queues */
    for (unsigned q = 0; q < l2fwd_queues_per_port; q++) {
      struct rte_eth_rxconf rxq_conf = dev_info.default_rxconf;
      rxq_conf.offloads = local_port_conf.rxmode.offloads;
      ret = rte_eth_rx_queue_setup(portid, q, nb_rxd,
                                   rte_eth_dev_socket_id(portid), &rxq_conf,
                                   l2fwd_pktmbuf_pool);
      if (ret < 0)
        rte_exit(EXIT_FAILURE,
                 "rte_eth_rx_queue_setup failed port=%u queue=%u\n", portid, q);
    }

    /* Setup TX Queues */
    for (unsigned q = 0; q < l2fwd_queues_per_port; q++) {
      struct rte_eth_txconf txq_conf = dev_info.default_txconf;
      txq_conf.offloads = local_port_conf.txmode.offloads;
      ret = rte_eth_tx_queue_setup(portid, q, nb_txd,
                                   rte_eth_dev_socket_id(portid), &txq_conf);
      if (ret < 0)
        rte_exit(EXIT_FAILURE,
                 "rte_eth_tx_queue_setup failed port=%u queue=%u\n", portid, q);
    }

    ret = rte_eth_dev_start(portid);
    if (ret < 0) {
      rte_exit(EXIT_FAILURE, "rte_eth_dev_start failed: err=%d, port=%u\n", ret,
               portid);
    }
    rte_eth_promiscuous_enable(portid);
  }

  /* Initialize TX buffers explicitly per lcore to map directly to their queue */
  for (lcore_id = 0; lcore_id < RTE_MAX_LCORE; lcore_id++) {
    struct lcore_queue_conf *qconf = &lcore_queue_conf[lcore_id];
    if (qconf->n_rx_port == 0)
      continue;

    RTE_ETH_FOREACH_DEV(portid) {
      if ((l2fwd_enabled_port_mask & (1 << portid)) == 0)
        continue;
      qconf->tx_buffer[portid] =
          rte_zmalloc_socket("tx_buffer", RTE_ETH_TX_BUFFER_SIZE(MAX_PKT_BURST),
                             0, rte_eth_dev_socket_id(portid));
      if (qconf->tx_buffer[portid] == NULL)
        rte_exit(EXIT_FAILURE, "Cannot allocate buffer\n");

      rte_eth_tx_buffer_init(qconf->tx_buffer[portid], MAX_PKT_BURST);
    }
  }

  memset(&port_statistics, 0, sizeof(port_statistics));

  if (l2fwd_maglev_enabled)
    maglev_init();
  else if (l2fwd_sashstore_enabled)
    sashstore_init();
  else if (l2fwd_dramblast_enabled)
    dramblast_init();

  samples = malloc(TOTAL_SAMPLES * sizeof(uint32_t));
  sleep(5);

  RTE_ETH_FOREACH_DEV(portid) {
    struct rte_eth_link link;
    if ((l2fwd_enabled_port_mask & (1 << portid)) == 0)
      continue;
    rte_eth_link_get(portid, &link); // Check Port 1
    if (link.link_status) {
      printf("Port %lu Link UP - %u Mbps\n", portid, link.link_speed);
    } else {
      printf("Port %lu Link DOWN\n", portid);
    }

    struct rte_ether_addr *mac = &l2fwd_ports_eth_addr[portid];
    printf("Port %u Physical MAC Address: %02X:%02X:%02X:%02X:%02X:%02X\n",
           portid, mac->addr_bytes[0], mac->addr_bytes[1], mac->addr_bytes[2],
           mac->addr_bytes[3], mac->addr_bytes[4], mac->addr_bytes[5]);
  }

  /* Use SKIP_MAIN so the main lcore stays available to process stats/timers exclusively */
  rte_eal_mp_remote_launch(l2fwd_launch_one_lcore, NULL, SKIP_MAIN);

  /* Stand-alone timer and statistics loop strictly for the main core */
  uint64_t prev_tsc = 0, cur_tsc = 0, start_tsc = 0, end_tsc = 0;
  start_tsc = rte_rdtsc();
  prev_tsc = start_tsc;
  end_tsc = start_tsc + rte_get_tsc_hz() * TOTAL_SAMPLES;

  while (!force_quit) {
    cur_tsc = rte_rdtsc();
    if (unlikely(cur_tsc - prev_tsc >= timer_period)) {
      print_stats();
      prev_tsc = cur_tsc;
    }

    if (cur_tsc >= end_tsc) {
      force_quit = true;
    }

    rte_pause();
  }

  print_final_stats();

  RTE_LCORE_FOREACH_WORKER(lcore_id) {
    if (rte_eal_wait_lcore(lcore_id) < 0) {
      ret = -1;
      break;
    }
  }

  RTE_ETH_FOREACH_DEV(portid) {
    if ((l2fwd_enabled_port_mask & (1 << portid)) == 0)
      continue;
    rte_eth_dev_stop(portid);
    rte_eth_dev_close(portid);
  }

  if (l2fwd_dramblast_enabled)
    dramblast_destroy();

  printf("Bye...\n");
  return ret;
}
