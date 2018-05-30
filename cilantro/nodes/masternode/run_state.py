from aiohttp import web
# from cilantro.nodes.masternode.masternode import MNBaseState, MNBootState
from cilantro.nodes.masternode.base_state import MNBaseState
from cilantro.nodes.masternode.boot_state import MNBootState
from cilantro.nodes.masternode.new_block_state import MNNewBlockState
from cilantro.messages import *
from cilantro.protocol.statemachine import *
from cilantro.protocol.structures import MerkleTree


class MNRunState(MNBaseState):
    NODE_AVAILABLE, NODE_AWAITING, NODE_TIMEOUT = range(3)

    def reset_attrs(self):
        self.block_contenders = []
        self.node_states = {}
        self.tx_hashes = []
        self.retrieved_txs = {}
        self.is_updating = False
        self.current_contender = None

    @enter_from(MNBootState)
    def enter_from_boot(self, prev_state):
        self.log.info("Starting web server")
        server = web.Server(self.parent.route_http)
        server_future = self.parent.loop.create_server(server, "0.0.0.0", 8080)
        self.parent.tasks.append(server_future)

    @enter_from(MNNewBlockState)  # TODO -- how do i do forward declations in python...?
    def enter_from_newblock_state(self, prev_state, success_status: bool, block_data):
        self.log.info("Entering RunState from NewBlock state with success status {} ...\n and block data {}"
                      .format(success_status, block_data))

    @input_request(BlockContender)
    def recv_block(self, block: BlockContender):
        self.log.info("Masternode received block contender: {}".format(block))
        self.log.critical("block nodes: {}".format(block.nodes))
        self.block_contenders.append(block)

        if self.current_contender:
            self.log.info("Masternode already executing new block update procedure")
            return

        self.current_contender = block
        self.is_updating = True
        self.log.critical("Masternode performing new block update procedure")

        # Compute hash of nodes, validate signatures
        hash_of_nodes = self.compute_hash_of_nodes(block.nodes)
        if not self.validate_sigs(block.signatures, hash_of_nodes):
            self.log.error("MN COULD NOT VALIDATE SIGNATURES FOR CONTENDER {}".format(block))
            # TODO -- remove this block from the queue and try the next (if any available)
            return

        self.tx_hashes = block.nodes[len(block.nodes) // 2:]

        assert len(block.nodes) >= 1, "Masternode got block contender with no nodes! {}".format(block)

        # Validate merkle tree
        if not MerkleTree.verify_tree(self.tx_hashes, hash_of_nodes):
            self.log.error("\n\n\n\nCOULD NOT VERIFY MERKLE TREE FOR BLOCK CONTENDER {}\n\n\n".format(block))
            # TODO -- remove this block from the queue and try the next (if any available)
            return

        # Add dealer sockets for Delegates to fetch block tx data
        for sig in block.signatures:
            vk = sig.sender
            self.node_states[sig.sender] = self.NODE_AVAILABLE
            self.parent.composer.add_dealer(vk=vk)
            import time
            time.sleep(0.1)

        repliers = list(self.node_states.keys())

        self.log.critical("block nodes: {}".format(block.nodes))

        # Request individual block data from delegates
        for i in range(len(self.tx_hashes)):
            tx = self.tx_hashes[i]
            replier_vk = repliers[i % len(repliers)]
            req = BlockDataRequest.create(tx)

            self.log.info("Requesting tx hash {} from VK {}".format(tx, replier_vk))
            self.parent.composer.send_request_msg(message=req, timeout=1, vk=replier_vk)

    def compute_hash_of_nodes(self, nodes) -> bytes:
        # TODO -- i think the merkle tree can do this for us..?
        self.log.info("Masternode computing hash of nodes...")
        h = hashlib.sha3_256()
        [h.update(o) for o in nodes]
        hash_of_nodes = h.digest()
        self.log.info("Masternode got hash of nodes: {}".format(hash_of_nodes))
        return hash_of_nodes

    def validate_sigs(self, signatures, msg) -> bool:
        for sig in signatures:
            self.log.info("mn verifying signature: {}".format(sig))
            if not sig.verify(msg, sig.sender):
                self.log.error("!!!! Oh no why couldnt we verify sig {}???".format(sig))
                return False
        return True

    @input(BlockDataReply)
    def recv_blockdata_reply(self, reply: BlockDataReply):
        if not self.is_updating:
            self.log.error("Received block data reply but not in updating state (reply={})".format(reply))
            return

        self.log.debug("masternode got block data reply: {}".format(reply))
        tx_hash = reply.tx_hash
        self.log.debug("BlockReply tx hash: {}".format(tx_hash))
        self.log.debug("Pending transactions: {}".format(self.tx_hashes))
        if tx_hash in self.tx_hashes:
            self.retrieved_txs[tx_hash] = reply.raw_tx
        else:
            self.log.error("Received block data reply with tx hash {} that is not in tx_hashes")

        # If we are done retreiving tranasctions, store the block
        if len(self.retrieved_txs) == len(self.tx_hashes):
            self.new_block_procedure()
        else:
            self.log.critical("Still {} transactions yet to request until we can build the block"
                              .format(len(self.tx_hashes) - len(self.retrieved_txs)))

    def new_block_procedure(self):
        self.log.critical("\n***\nDONE COLLECTING BLOCK DATA FROM NODES\n***\n")

        block = self.current_contender

        hash_of_nodes = self.compute_hash_of_nodes(block.nodes).hex()
        tree = b"".join(block.nodes).hex()
        signatures = "".join([merk_sig.signature for merk_sig in block.signatures])

        # Store the block + transaction data
        block_num = -1
        with DB() as db:
            tables = db.tables
            q = insert(tables.blocks).values(hash=hash_of_nodes, tree=tree, signatures=signatures)
            q_result = db.execute(q)
            block_num = q_result.lastrowid

            for key, value in self.retrieved_txs.items():
                tx = {
                    'key': key,
                    'value': value
                }
                qq = insert(tables.transactions).values(tx)
                db.execute(qq)

        assert block_num > 0, "Block num must be greater than 0! Was it not set in the DB() context session?"

        # Notify delegates of new block
        self.log.info("Masternode sending NewBlockNotification to delegates with new block hash {} and block num {}"
                      .format(hash_of_nodes, block_num))
        notif = NewBlockNotification.create(new_block_hash=hash_of_nodes, new_block_num=block_num)
        self.parent.composer.send_pub_msg(filter=Constants.ZmqFilters.MasternodeDelegate, message=notif)

        # Reset block update ivars
        self.reset_attrs()

    @input_timeout(BlockDataRequest)
    def timeout_block_req(self, request: BlockDataRequest, envelope: Envelope):
        self.log.info("\n\nBlockDataRequest timed out for envelope with request data {}\n\n".format(envelope, request))

    @input_request(StateRequest)
    def handle_state_req(self, request: StateRequest):
        self.log.info("Masternode got state request {}".format(request))