"""Compile and exercise the production C++ daemon against a test-only SDK; no hardware."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import numpy as np
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
import devices.asi_camera as driver


def main():
    with tempfile.TemporaryDirectory(prefix='spectral-camd-test-') as tmp:
        root=Path(tmp)
        exe=root/'asi_camd'
        src=BASE/'hsi/lamda_hsi'
        subprocess.run(['c++','-std=c++17','-pthread','-D_MAC' if sys.platform=='darwin' else '-D_LIN',
                        '-I'+str(src/'ASI_linux_mac_SDK_V1.41/include'),
                        str(src/'src/asi_camd.cpp'),str(src/'src/camera.cpp'),
                        str(BASE/'tests/fake_asi_sdk.cpp'),'-o',str(exe)],check=True)
        original=driver.CAMD_EXE
        driver.CAMD_EXE=exe
        try:
            with_camera(root)
        finally:
            driver.CAMD_EXE=original
            os.environ.pop('FAKE_ASI_STOP_MARKER',None)
            os.environ.pop('FAKE_ASI_HANG',None)
    print('ALL C++ CAMERA TESTS PASSED (fake SDK, no hardware)')


def with_camera(root):
    cam=driver.ASICameraClient(log_path=root/'camera.log')
    try:
        assert cam.info['sdk']=='fake-sdk-test-only'
        assert cam.info['max_intensity']==65532
        cam.configure(2000,100,50,1,(0,0,64,32))
        cam.expose(root/'raw.npy')
        raw=np.load(root/'raw.npy')
        assert raw.dtype==np.uint16 and raw.shape==(32,64)
        assert np.array_equal(raw.ravel(),np.arange(2048)+1000)
        before=(root/'raw.npy').read_bytes()
        try:
            cam.expose(root/'raw.npy')
            raise AssertionError('overwrite accepted')
        except FileExistsError:
            pass
        assert (root/'raw.npy').read_bytes()==before
    finally:
        cam.close()
    for hang in (False,True):
        marker=root/f'stop-{hang}'
        os.environ['FAKE_ASI_STOP_MARKER']=str(marker)
        if hang:os.environ['FAKE_ASI_HANG']='1'
        cam=driver.ASICameraClient(log_path=root/'camera.log')
        errors=[]
        try:
            cam.configure(10_000_000,100,50,1,(0,0,64,32))
            def exposure():
                try:cam.expose(root/f'aborted-{hang}.npy')
                except driver.CameraError as exc:errors.append(str(exc))
            t=threading.Thread(target=exposure);t.start();time.sleep(.2)
            start=time.monotonic();cam.abort();t.join(5)
            assert not t.is_alive() and time.monotonic()-start<4.5 and errors
            assert not (root/f'aborted-{hang}.npy').exists()
            if not hang:assert marker.read_text()=='stopped'
        finally:
            cam.close()
            os.environ.pop('FAKE_ASI_HANG',None)


if __name__=='__main__':main()
