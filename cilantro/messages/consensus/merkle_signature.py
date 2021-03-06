from cilantro import Constants
from cilantro.messages import MessageBase
from cilantro.messages.utils import validate_hex
import json


"""
MerkleSignatures are exchanged among delegates during consensus to verify if they all have the same data.
When a delegate starts consensus, it builds a Merkle tree with its entire transaction queue (all transaction that would
be in the proposed block), signs the hash of the merkle tree, and creates a MerkleSignature with the resulting signature
as well as the delegates id and a timestamp.

Other delegates receive this MerkleSignature, and attempt to verify it using the sender's verifying key and the receiving
delegate's own merkle hash (which should be the same as the sender's merkle hash if their blocks have the same 
transactions).

Once a delegate has enough signature, it creates a BlockContender and sends it to Masternode. The BlockContender
contains the merkle tree as well as a list of these collected MerkleSignatures. 

TODO -- switch this class to use capnp
"""


class MerkleSignature(MessageBase):
    """
    _data is a dict with keys: 'signature', 'timestamp', 'sender'
    """
    SIG = 'signature'
    TS = 'timestamp'
    SENDER = 'sender'

    def __eq__(self, other_ms):
        """Check two merkle signatures have identical features"""
        return self._data == other_ms._data

    def validate(self):
        assert type(self._data) == dict, "_data is not a dictionary"
        assert self.SIG in self._data, "Signature field missing from _data: {}".format(self._data)
        assert self.TS in self._data, "Timestamp field missing from _data: {}".format(self._data)
        assert self.SENDER in self._data, "Sender field missing from _data: {}".format(self._data)

        validate_hex(self._data[self.SIG], 128, self.SIG)
        validate_hex(self._data[self.SENDER], 64, self.SENDER)
        # Validate timestamp somehow?

    def serialize(self):
        return json.dumps(self._data).encode()

    def verify(self, msg, verifying_key):
        if validate_hex(verifying_key, length=64, raise_err=False):
            return Constants.Protocol.Wallets.verify(verifying_key, msg, self.signature)
        else:
            return False

    @classmethod
    def create(cls, sig_hex: str, timestamp: str, sender: str, validate=True):
        data = {cls.SIG: sig_hex, cls.TS: timestamp, cls.SENDER: sender}
        return cls.from_data(data, validate=validate)

    @classmethod
    def _deserialize_data(cls, data: bytes):
        return json.loads(data.decode())

    @property
    def signature(self):
        return self._data[self.SIG]

    @property
    def timestamp(self):
        return self._data[self.TS]

    @property
    def sender(self):
        return self._data[self.SENDER]
