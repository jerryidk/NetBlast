#include "hashmap.h"
#include "packettool.h"
#include "sashstore.h"

char STORED[ ] = {'S', 'T', 'O', 'R', 'E', 'D'};
char VALUE[ ] = {'V', 'A', 'L', 'U', 'E'};
char END[ ] = {'E', 'N', 'D'};

static struct sashstore_hashmap kvstore = {};

struct header {
	uint8_t req_id[4];
	uint8_t seq_nr[2];
	uint8_t dgram_tot[2];
	uint8_t rsvd[2];
} __attribute__((packed));

struct request_hdr {
	struct header header;
	uint8_t mode_str[4];
} __attribute__((packed));

struct set_request {
	uint8_t key[KEY_SIZE];
	uint8_t space;
	uint8_t flag[4];
	uint8_t len[4];
	uint8_t line_end[2];
	uint8_t value[VALUE_SIZE];
	uint8_t line_end1[2];
} __attribute__((packed));

struct get_request {
	uint8_t key[KEY_SIZE];
	uint8_t line_end[2];
} __attribute__((packed));

struct set_response {
	struct header header;
	uint8_t value[sizeof(STORED)];
	uint8_t end_str[2];
} __attribute__((packed));

struct get_response {
	struct header header;
	uint8_t value_str[sizeof(VALUE)];
	uint8_t space;
	uint8_t key[KEY_SIZE];
	uint8_t space1;
	uint8_t flag[4];
	uint8_t len[4];
	uint8_t line_end[2];
	uint8_t value[VALUE_SIZE];
	uint8_t line_end1[2];
	uint8_t end_str[sizeof(END)];
	uint8_t line_end2[2];
} __attribute__((packed));

typedef enum {
	SET_REQUEST = 1,
	GET_REQUEST = 2,
} request_type_t;

static uint64_t total_set, total_get, total_stored, total_not_stored, total_retrieved, total_not_found;

int64_t handle_network_request(void *payload) {
	struct request_hdr *hdr = (struct request_hdr *) payload;
	int64_t ret = -1;

	if (!memcmp(hdr->mode_str, "set ", 4)) {
		struct set_request *set = (struct set_request *) &hdr[1];
		// handle set request
		struct sashstore_kv_pair *pair = sashstore_hashmap_insert(&kvstore, set->key, set->value);


		if (pair) {
			// construct successful set response
			struct set_response *resp = (struct set_response *) payload;
			memcpy(resp->value, STORED, sizeof(STORED)); 
			ret = sizeof(struct set_response);
			total_stored++;
		} else {
			//printf("not stored req k: %s, v: %s", set->key, set->value);
			strncpy(payload, "NOT_STORED", strlen("NOT_STORED") + 1);
			total_not_stored++;
			ret = strlen("NOT_STORED");
		}
		total_set++;
	} else if (!memcmp(hdr->mode_str, "get ", 4)) {
		// handle get request
		struct get_request *get = (struct get_request *) &hdr[1];
		// handle set request
		struct sashstore_kv_pair *pair = sashstore_hashmap_get(&kvstore,
					get->key);

		//printf("get req\n");

		if (pair) {
			// construct successful get response
			struct get_response *resp = (struct get_response *) payload;
			memcpy(resp->value_str, VALUE, sizeof(VALUE));
			memcpy(resp->key, get->key, KEY_SIZE);
			memcpy(resp->value, pair->value, VALUE_SIZE);
			memcpy(resp->end_str, END, sizeof(END));
			ret = sizeof(struct get_response);
			total_retrieved++;
		} else {	
			//printf("not_found %s", get->key);
			ret = strlen("NOT_FOUND");
			strncpy(payload, "NOT_FOUND", strlen("NOT_FOUND") + 1);
			total_not_found++;
		}	
		total_get++;
	} else {
		printf("%s\n", hdr->mode_str);
	}
	return ret;
}

int64_t sashstore_process_frame(void *frame, size_t len) {
	(void) len;
	void *payload = get_udp_payload((char *) frame);
	int64_t ret = -1;
	if (payload) {
		ret = handle_network_request(payload);
	} else {
		printf("malformed packet\n");
	}
	//printf("handle_nw_request %d\n", ret);
	return ret;
}

void sashstore_init(void) {
	size_t size = CAPACITY * sizeof(struct sashstore_kv_pair);
	kvstore.pairs = aligned_alloc(4096, size);

	if (!kvstore.pairs) {
		printf("Aligned alloc failed!\n");
		exit(1);
	}
}

void print_sashstore_stats(uint64_t start, uint64_t end) {

	printf("key_size %u, value_size %u, capacity %llu\n",
			KEY_SIZE, VALUE_SIZE, CAPACITY);
	printf("total_set %lu, total_stored %lu total_not_stored %lu\n"
		"total_get %lu, total_retrieved %lu total_not_found %lu\n",
		total_set, total_stored, total_not_stored,
		total_get, total_retrieved, total_not_found);
}
