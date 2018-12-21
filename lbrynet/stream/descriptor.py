import os
import json
import binascii
import logging
import typing
import asyncio
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from lbrynet.blob import MAX_BLOB_SIZE
from lbrynet.blob.blob_info import BlobInfo
from lbrynet.blob.blob_file import BlobFile
from lbrynet.cryptoutils import get_lbry_hash_obj
from lbrynet.error import InvalidStreamDescriptorError
from lbrynet.storage import SQLiteStorage


log = logging.getLogger(__name__)


def format_sd_info(stream_name: str, key: str, suggested_file_name: str, stream_hash: str,
                   blobs: typing.List[typing.Dict]) -> typing.Dict:
    return {
        "stream_type": "lbryfile",
        "stream_name": stream_name,
        "key": key,
        "suggested_file_name": suggested_file_name,
        "stream_hash": stream_hash,
        "blobs": blobs
    }


def random_iv_generator() -> typing.Generator[bytes, None, None]:
    while 1:
        yield os.urandom(AES.block_size // 8)


async def file_reader(file_path: str):
    length = int(os.stat(file_path).st_size)
    offset = 0

    with open(file_path, 'rb') as stream_file:
        while offset < length:
            bytes_to_read = min((length - offset), MAX_BLOB_SIZE - 1)
            if not bytes_to_read:
                break
            blob_bytes = stream_file.read(bytes_to_read)
            yield blob_bytes
            offset += bytes_to_read


class StreamDescriptor:
    def __init__(self, stream_name: str, key: str, suggested_file_name: str, blobs: typing.List[BlobInfo],
                 stream_hash: typing.Optional[str] = None, sd_hash: typing.Optional[str] = None):
        self.stream_name = stream_name
        self.key = key
        self.suggested_file_name = suggested_file_name
        self.blobs = blobs
        self.stream_hash = stream_hash or self.get_stream_hash()
        self.sd_hash = sd_hash

    def get_stream_hash(self) -> str:
        return self.calculate_stream_hash(
            binascii.hexlify(self.stream_name.encode()), self.key.encode(),
            binascii.hexlify(self.suggested_file_name.encode()),
            [blob_info.as_dict() for blob_info in self.blobs]
        )

    def calculate_sd_hash(self) -> str:
        h = get_lbry_hash_obj()
        h.update(self.as_json())
        return h.hexdigest()

    def as_json(self) -> bytes:
        return json.dumps(
            format_sd_info(binascii.hexlify(self.stream_name.encode()).decode(), self.key,
                           binascii.hexlify(self.suggested_file_name.encode()).decode(),
                           self.stream_hash,
                           [blob_info.as_dict() for blob_info in self.blobs]), sort_keys=True
        ).encode()

    async def write_sd_blob_file(self, loop: asyncio.BaseEventLoop, blob_dir: str):
        def _write_sd_blob_file():
            self.sd_hash = self.calculate_sd_hash()
            path = os.path.join(blob_dir, self.sd_hash)
            if os.path.exists(path):
                raise IOError("sd blob exists")
            with open(path, 'wb') as sd_file:
                sd_file.write(self.as_json())
        return await loop.run_in_executor(None, _write_sd_blob_file)

    @classmethod
    def _from_stream_descriptor_blob(cls, blob: BlobFile):
        assert os.path.isfile(blob.file_path)
        with open(blob.file_path, 'rb') as f:
            json_bytes = f.read()
        decoded = json.loads(json_bytes.decode())
        if decoded['blobs'][-1]['length'] != 0:
            raise InvalidStreamDescriptorError("Does not end with a zero-length blob.")
        if any([blob_info['length'] == 0 for blob_info in decoded['blobs'][:-1]]):
            raise InvalidStreamDescriptorError("Contains zero-length data blob")
        if 'blob_hash' in decoded['blobs'][-1]:
            raise InvalidStreamDescriptorError("Stream terminator blob should not have a hash")
        descriptor = cls(
            binascii.unhexlify(decoded['stream_name']).decode(),
            decoded['key'],
            binascii.unhexlify(decoded['suggested_file_name']).decode(),
            [BlobInfo(info['blob_num'], info['length'], info['iv'], info.get('blob_hash'))
             for info in decoded['blobs']],
            decoded['stream_hash']
        )
        if descriptor.get_stream_hash() != decoded['stream_hash']:
            raise InvalidStreamDescriptorError("Stream hash does not match stream metadata")
        return descriptor

    @classmethod
    async def from_stream_descriptor_blob(cls, loop: asyncio.BaseEventLoop, blob):
        return await loop.run_in_executor(None, lambda: cls._from_stream_descriptor_blob(blob))

    @staticmethod
    def get_blob_hashsum(b: typing.Dict):
        length = b['length']
        if length != 0:
            blob_hash = b['blob_hash']
        else:
            blob_hash = None
        blob_num = b['blob_num']
        iv = b['iv']
        blob_hashsum = get_lbry_hash_obj()
        if length != 0:
            blob_hashsum.update(blob_hash.encode())
        blob_hashsum.update(str(blob_num).encode())
        blob_hashsum.update(iv.encode())
        blob_hashsum.update(str(length).encode())
        return blob_hashsum.digest()

    @staticmethod
    def calculate_stream_hash(hex_stream_name: bytes, key: bytes, hex_suggested_file_name: bytes,
                        blob_infos: typing.List[typing.Dict]) -> str:
        h = get_lbry_hash_obj()
        h.update(hex_stream_name)
        h.update(key)
        h.update(hex_suggested_file_name)
        blobs_hashsum = get_lbry_hash_obj()
        for blob in blob_infos:
            blobs_hashsum.update(StreamDescriptor.get_blob_hashsum(blob))
        h.update(blobs_hashsum.digest())
        return h.hexdigest()

    async def save_to_database(self, loop: asyncio.BaseEventLoop, storage: SQLiteStorage):
        sd_hash = self.sd_hash or self.calculate_sd_hash()
        blob_dicts = [b.as_dict() for b in self.blobs]
        await storage.add_known_blobs(blob_dicts).asFuture(loop)
        await storage.store_stream(
            self.stream_hash, sd_hash, binascii.hexlify(self.stream_name.encode()).decode(),
            self.key, binascii.hexlify(self.suggested_file_name.encode()).decode(), blob_dicts
        ).asFuture(loop)

    @classmethod
    async def create_stream(cls, loop: asyncio.BaseEventLoop, storage: SQLiteStorage, blob_dir: str, file_path: str,
                            key: typing.Optional[bytes] = None,
                            iv_generator: typing.Optional[typing.Generator[bytes, None, None]] = None,
                            create_limit: typing.Optional[int] = 20):

        blobs: typing.List[BlobInfo] = []

        def wrote_blob_callback(blob_hash: str, blob_iv: bytes, length: int, num: int) -> None:
            blobs.append(BlobInfo(num, length, binascii.hexlify(blob_iv).decode(), blob_hash))
            return

        iv_generator = iv_generator or random_iv_generator()
        key = key or os.urandom(AES.block_size // 8)
        batch = []
        blob_num = 0

        async for blob_bytes in file_reader(file_path):
            batch.append(
                BlobFile.create_from_unencrypted(
                    loop, blob_dir, key, next(iv_generator), blob_bytes, wrote_blob_callback, blob_num
                )
            )
            blob_num += 1
            if len(batch) >= create_limit:  # create the batch of blobs
                tasks = []
                while batch:
                    tasks.append(batch.pop())
                await asyncio.gather(*tuple(tasks), loop=loop)
        if batch:  # create any remaining blobs
            await asyncio.gather(*tuple(batch), loop=loop)

        blobs.sort(key=lambda blob_info: blob_info.blob_num)  # sort by blob number
        blobs.append(
            BlobInfo(len(blobs), 0, binascii.hexlify(next(iv_generator)).decode()))  # add the stream terminator
        descriptor = cls(
            os.path.basename(file_path), binascii.hexlify(key).decode(), os.path.basename(file_path), blobs
        )
        await descriptor.write_sd_blob_file(loop, blob_dir)
        await descriptor.save_to_database(loop, storage)
        return descriptor