"""Bounded JSON decoding of archive arrays; no whole-document Unicode copy."""
import json
import sys
from functools import lru_cache

# Only repeated metadata, never bodies/permalinks. The cache itself is bounded.
METADATA={'platform','origin','source_id','source_url','source_name',
          'published_at','observed_at','collected_at','discovered_at',
          'imported_at','updated_at','first_archived_at','category','region','language','tool'}

@lru_cache(maxsize=8192)
def shared_metadata(value):
    return value


def compact_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate_json_key')
        if key in METADATA and isinstance(value,str):value=shared_metadata(value)
        result[sys.intern(key)]=value
    # The wire format carries both names for existing readers. Share identical
    # immutable text in RAM instead of retaining two independently decoded copies.
    if isinstance(result.get('text'),str) and result.get('content')==result['text']:
        result['content']=result['text']
    return result


class Reader:
    def __init__(self,stream,chunk=65536):
        self.stream,self.chunk=stream,chunk
        self.buffer='';self.position=0;self.eof=False
        self.decoder=json.JSONDecoder(object_pairs_hook=compact_object)

    def more(self):
        self.buffer=self.buffer[self.position:];self.position=0
        part=self.stream.read(self.chunk)
        self.buffer+=part
        if not part:self.eof=True

    def peek(self):
        while True:
            while self.position<len(self.buffer) and self.buffer[self.position] in ' \r\n\t':self.position+=1
            if self.position<len(self.buffer):return self.buffer[self.position]
            if self.eof:return ''
            self.more()

    def take(self,expected):
        if self.peek()!=expected:raise ValueError('invalid_archive_json')
        self.position+=1

    def value(self):
        self.peek()
        # Discard previous values before growing a token across chunk boundaries.
        self.buffer=self.buffer[self.position:];self.position=0
        while True:
            try:
                value,end=self.decoder.raw_decode(self.buffer)
                if not self.eof and (end==len(self.buffer) or self.buffer[end] not in ' \r\n\t,]}:'):
                    self.more();continue
                self.position=end
                return value
            except json.JSONDecodeError:
                if self.eof:raise
                self.more()


def load(stream,*,item_consumer=None,chunk=65536):
    """Decode top-level arrays one entry at a time; optionally compare items only."""
    reader=Reader(stream,chunk)
    if reader.peek()!='{':
        result=reader.value()
    else:
        result={};reader.take('{')
        if reader.peek()!='}':
            while True:
                key=reader.value()
                if not isinstance(key,str) or key in result:raise ValueError('invalid_archive_json_key')
                reader.take(':')
                if reader.peek()=='[':
                    reader.take('[');values=[]
                    if reader.peek()!=']':
                        while True:
                            value=reader.value()
                            if key=='items' and item_consumer is not None:item_consumer(value)
                            else:values.append(value)
                            if reader.peek()!=',':break
                            reader.take(',')
                    reader.take(']');result[key]=values
                else:result[key]=reader.value()
                if reader.peek()!=',':break
                reader.take(',')
        reader.take('}')
    if reader.peek():raise ValueError('trailing_archive_json')
    return result
