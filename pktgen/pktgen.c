#include <rte_eal.h>
#include <rte_ethdev.h>
#include <rte_mbuf.h>
#include <rte_ether.h>
#include <rte_ip.h>
#include <rte_udp.h>
#include <rte_lcore.h>
#include <unistd.h>    // Required for sleep()
#include <signal.h>    // Required for signal handling
#include <stdbool.h>   // Required for bool type
#include <getopt.h>    // Required for parsing -r
#include <stdlib.h>    // Required for atoi()

#define NUM_MBUFS_PER_CORE 8191
#define MBUF_CACHE_SIZE 250
#define BURST_SIZE 32
#define PAYLOAD_PADDING_SIZE 18

static const struct rte_eth_conf port_conf_default = {
    .rxmode = { .max_lro_pkt_size = RTE_ETHER_MAX_LEN }
};

/* Global termination flag */
static volatile bool force_quit = false;

/* Global mask for IP range (-r) */
static uint32_t g_ip_mask = 0xFF; // Default to 256 IPs (mask 255)

/* Structure containing configuration for each worker lcore */
struct lcore_conf {
    uint16_t port_id;
    uint16_t tx_queue_id;
    struct rte_mempool *mbuf_pool;
    struct rte_ether_addr port_mac;
} __rte_cache_aligned;

static struct lcore_conf lcore_config[RTE_MAX_LCORE];

/* Signal handler function to intercept Ctrl+C / SIGTERM */
static void signal_handler(int signum) {
    if (signum == SIGINT || signum == SIGTERM) {
        printf("\n\nSignal %d received, preparing for safe termination...\n", signum);
        force_quit = true;
    }
}

/* Fast path Tx Main Loop executed on every worker core */
static int tx_worker_loop(__rte_unused void *arg) {
    unsigned int lcore_id = rte_lcore_id();
    struct lcore_conf *conf = &lcore_config[lcore_id];

    uint16_t portid = conf->port_id;
    uint16_t queueid = conf->tx_queue_id;
    struct rte_mempool *mbuf_pool = conf->mbuf_pool;

    uint32_t base_ip = RTE_IPV4(10, lcore_id, 0, 0);
    uint32_t client_ip_counter = 1;
    uint16_t client_port_counter = 1025 + (lcore_id * 100);

    printf("Core %u spinning up: Transmitting on Port %u, TX Queue %u (IP Mask: 0x%X)\n",
           lcore_id, portid, queueid, g_ip_mask);

    /* Loop safely breaks when force_quit becomes true */
    while (!force_quit) {
        struct rte_mbuf *bufs[BURST_SIZE];

        /* Allocate from the core's isolated mbuf pool */
        if (unlikely(rte_pktmbuf_alloc_bulk(mbuf_pool, bufs, BURST_SIZE) != 0)) {
            continue;
        }

        for (int i = 0; i < BURST_SIZE; i++) {
            struct rte_mbuf *m = bufs[i];

            char *data = rte_pktmbuf_append(m, sizeof(struct rte_ether_hdr) +
                                               sizeof(struct rte_ipv4_hdr) +
                                               sizeof(struct rte_udp_hdr) +
                                               PAYLOAD_PADDING_SIZE);

            struct rte_ether_hdr *eth = (struct rte_ether_hdr *)data;
            struct rte_ipv4_hdr *ip = (struct rte_ipv4_hdr *)(eth + 1);
            struct rte_udp_hdr *udp = (struct rte_udp_hdr *)(ip + 1);

            memset(eth, 0, sizeof(struct rte_ether_hdr) + sizeof(struct rte_ipv4_hdr) + sizeof(struct rte_udp_hdr));

            // Layer 2
            memset(&eth->dst_addr, 0xFF, 6);
            rte_ether_addr_copy(&conf->port_mac, &eth->src_addr);
            eth->ether_type = rte_cpu_to_be_16(RTE_ETHER_TYPE_IPV4);

            // Layer 3 (Mutate client IP within the power-of-2 mask)
            uint32_t current_ip = base_ip + (client_ip_counter & g_ip_mask);
            client_ip_counter++;

            ip->version_ihl = 0x45;
            ip->total_length = rte_cpu_to_be_16(sizeof(struct rte_ipv4_hdr) +
                                               sizeof(struct rte_udp_hdr) +
                                               PAYLOAD_PADDING_SIZE);
            ip->next_proto_id = IPPROTO_UDP;
            ip->src_addr = rte_cpu_to_be_32(current_ip);
            ip->dst_addr = rte_cpu_to_be_32(RTE_IPV4(192, 168, 1, 1));
            ip->time_to_live = 64;         // FIX: Set a valid TTL
            ip->fragment_offset = 0;       // FIX: explicitly no fragmentation
            ip->packet_id = 0;

            ip->hdr_checksum = rte_ipv4_cksum(ip);

            // Layer 4 (Mutate client Port)
            udp->src_port = rte_cpu_to_be_16(client_port_counter);
            udp->dst_port = rte_cpu_to_be_16(80);
            udp->dgram_len = rte_cpu_to_be_16(sizeof(struct rte_udp_hdr) + PAYLOAD_PADDING_SIZE);
            udp->dgram_cksum = 0;

            char *payload = (char *)(udp + 1);
            memset(payload, 0, PAYLOAD_PADDING_SIZE);

            if (client_port_counter > 65000) {
                client_port_counter = 1025;
            }
        }

        uint16_t nb_tx = rte_eth_tx_burst(portid, queueid, bufs, BURST_SIZE);

        if (unlikely(nb_tx < BURST_SIZE)) {
            for (uint16_t buf = nb_tx; buf < BURST_SIZE; buf++) {
                rte_pktmbuf_free(bufs[buf]);
            }
        }
    }

    printf("Core %u spinning down cleanly.\n", lcore_id);
    return 0;
}

int main(int argc, char *argv[]) {
    int ret = rte_eal_init(argc, argv);
    if (ret < 0) rte_exit(EXIT_FAILURE, "Error with EAL initialization\n");

    /* Adjust argc and argv to parse application-specific arguments */
    argc -= ret;
    argv += ret;

    int opt;
    while ((opt = getopt(argc, argv, "r:")) != -1) {
        if (opt == 'r') {
            int range = atoi(optarg);
            // Verify it is strictly a power of 2 and greater than 0
            if (range <= 0 || (range & (range - 1)) != 0) {
                rte_exit(EXIT_FAILURE, "Invalid range: -r must be a power of 2 (e.g., 2, 4, 128, 256)\n");
            }
            g_ip_mask = range - 1;
        }
    }

    /* Register the signal handler patterns */
    force_quit = false;
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    uint16_t nb_ports = rte_eth_dev_count_avail();
    if (nb_ports == 0) rte_exit(EXIT_FAILURE, "No Ethernet ports detected\n");

    printf("Detected %u usable network port(s).\n", nb_ports);

    uint16_t tx_queues_per_port[RTE_MAX_ETHPORTS] = {0};
    unsigned int lcore_id;
    uint16_t port_rr = 0;

    RTE_LCORE_FOREACH_WORKER(lcore_id) {
        lcore_config[lcore_id].port_id = port_rr;
        lcore_config[lcore_id].tx_queue_id = tx_queues_per_port[port_rr];

        rte_eth_macaddr_get(port_rr, &lcore_config[lcore_id].port_mac);
        tx_queues_per_port[port_rr]++;
        port_rr = (port_rr + 1) % nb_ports;
    }

    uint16_t portid;
    RTE_ETH_FOREACH_DEV(portid) {
        uint16_t n_tx_queue = tx_queues_per_port[portid];
        if (n_tx_queue == 0) {
            printf("Warning: Port %u has no cores assigned to it. Skipping.\n", portid);
            continue;
        }

        printf("Configuring Port %u with %u hardware TX queues...\n", portid, n_tx_queue);

        if (rte_eth_dev_configure(portid, 0, n_tx_queue, &port_conf_default) < 0) {
            rte_exit(EXIT_FAILURE, "Cannot configure port %u\n", portid);
        }

        for (uint16_t q = 0; q < n_tx_queue; q++) {
            if (rte_eth_tx_queue_setup(portid, q, 1024, rte_eth_dev_socket_id(portid), NULL) < 0) {
                rte_exit(EXIT_FAILURE, "Port %u TX queue %u setup failed\n", portid, q);
            }
        }

        if (rte_eth_dev_start(portid) < 0) {
            rte_exit(EXIT_FAILURE, "Cannot start port %u\n", portid);
        }
    }

    printf("Waiting 4 seconds for physical links to come up...\n");
    sleep(4);

    RTE_LCORE_FOREACH_WORKER(lcore_id) {
        char pool_name[32];
        snprintf(pool_name, sizeof(pool_name), "MBUF_POOL_C%u", lcore_id);

        struct rte_mempool *mp = rte_pktmbuf_pool_create(
            pool_name, NUM_MBUFS_PER_CORE, MBUF_CACHE_SIZE, 0,
            RTE_MBUF_DEFAULT_BUF_SIZE, rte_lcore_to_socket_id(lcore_id));

        if (mp == NULL) {
            rte_exit(EXIT_FAILURE, "Cannot init mbuf pool for core %u\n", lcore_id);
        }
        lcore_config[lcore_id].mbuf_pool = mp;
    }

    printf("Master core finished mapping. Launching workers...\n");
    rte_eal_mp_remote_launch(tx_worker_loop, NULL, SKIP_MAIN);

    /* Wait for all worker threads to return after force_quit switches to true */
    rte_eal_mp_wait_lcore();

    /* Clean up hardware state before returning control to the OS */
    printf("Stopping and closing network ports...\n");
    RTE_ETH_FOREACH_DEV(portid) {
        if (tx_queues_per_port[portid] == 0) continue;

        printf("Closing Port %u...\n", portid);
        rte_eth_dev_stop(portid);
        rte_eth_dev_close(portid);
    }

    printf("DPDK clean exit completed successfully.\n");
    return 0;
}
