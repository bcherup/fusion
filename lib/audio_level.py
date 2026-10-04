"""Bounded, read-only PCM measurements; these are file levels, not handset volume."""
import array
import hashlib
import io
import math
import sys
import wave
from .common import need


def measure(path):
    need(path.stat().st_size<=64*1024**2,'Music track exceeds the measurement limit')
    data=path.read_bytes()
    try:source=wave.open(io.BytesIO(data),'rb')
    except (wave.Error,EOFError) as error:
        from .common import Error
        raise Error('A music file is not a readable WAV; inspect the selected collection') from error
    with source:
        need(source.getsampwidth()==2 and source.getcomptype()=='NONE','Music measurement requires PCM16 WAV')
        count=0;energy=0;peak=0
        while block:=source.readframes(65536):
            values=array.array('h',block)
            if sys.byteorder!='little':values.byteswap()
            count+=len(values);energy+=math.sumprod(values,values)
            peak=max(peak,abs(min(values)),abs(max(values)))
        need(count>0,'Music track contains no audio samples')
    return {'samples':count,'energy':energy,'peak':peak,'sha256':hashlib.sha256(data).hexdigest()}


def combine(values):
    count=sum(x['samples'] for x in values);energy=sum(x['energy'] for x in values)
    peak=max(x['peak'] for x in values)
    db=lambda value:round(20*math.log10(value/32768),1) if value else None
    return {'rms_dbfs':db(math.sqrt(energy/count)),'peak_dbfs':db(peak),'silent':peak==0}
