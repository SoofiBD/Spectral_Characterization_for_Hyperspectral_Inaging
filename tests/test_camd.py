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
            startup_failures(root)
            device_loss(root)
            rejected_control(root)
        finally:
            driver.CAMD_EXE=original
            for key in ('FAKE_ASI_STOP_MARKER','FAKE_ASI_HANG','FAKE_ASI_INIT_ERROR','FAKE_ASI_CLOSE_MARKER',
                        'FAKE_ASI_USB2_HOST','FAKE_ASI_NO_CAMERA','FAKE_ASI_REMOVE_AFTER','FAKE_ASI_EXPOSE_ERROR',
                        'FAKE_ASI_FAIL_CLOSE_AFTER'):
                os.environ.pop(key,None)
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
        from acquisition.camera_measurement import record_camera_frame
        for i in range(3):  # live-preview pattern on the real daemon: expose -> load -> delete
            preview=root/f'preview_{i}.npy'
            cam.expose(preview)
            assert np.load(preview).shape==(32,64)
            preview.unlink()
        assert not list(root.glob('preview_*'))
        session=record_camera_frame(cam,root/'frame_session')
        rec=session.manifest['points'][0]['camera']
        assert session.manifest['status']=='complete' and rec['dtype']=='uint16' and rec['shape']==[32,64]
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


def startup_failures(root):
    """A failed open is reported with the numeric SDK code and camera/USB path, logged before any
    open, and releases the camera; the client reaps the process and closes its log."""
    log=root/'startup.log'
    marker=root/'closed'
    os.environ.update(FAKE_ASI_INIT_ERROR='16',FAKE_ASI_CLOSE_MARKER=str(marker),FAKE_ASI_USB2_HOST='1')
    try:
        driver.ASICameraClient(log_path=log)
        raise AssertionError('init failure accepted')
    except driver.CameraError as exc:
        message=str(exc)
    assert message==('ASIInitCamera: general error (ASI error 16) '
                     '[Test RAW16 camera, index 0, ID 0, USB3 host no]'),message
    assert marker.read_text()=='closed'
    text=log.read_text()
    assert 'ASIGetNumOfConnectedCameras = 1' in text and 'USB3 camera yes, USB3 host no' in text, text
    assert 'startup failed: ASIInitCamera' in text, text
    for key in ('FAKE_ASI_INIT_ERROR','FAKE_ASI_CLOSE_MARKER','FAKE_ASI_USB2_HOST'):
        os.environ.pop(key)
    os.environ['FAKE_ASI_NO_CAMERA']='1'
    try:
        driver.ASICameraClient(log_path=log)
        raise AssertionError('missing camera accepted')
    except driver.CameraError as exc:
        assert str(exc)=='no ASI camera found (SDK reported 0 connected)',exc
    finally:
        os.environ.pop('FAKE_ASI_NO_CAMERA')
    cam=driver.ASICameraClient(log_path=log)  # the next start opens the camera normally
    try:
        assert cam.info['model']=='Test RAW16 camera'
    finally:
        cam.close()
    print('startup failures: numeric code, USB path, log and camera release OK')


def rejected_control(root):
    os.environ['FAKE_ASI_REJECT_CONTROL']='1'  # ASI_EXPOSURE
    cam=driver.ASICameraClient(log_path=root/'camera.log')
    try:
        try:
            cam.configure(1,100,50,1,(0,0,64,32))
            raise AssertionError('rejected exposure accepted')
        except driver.CameraError as exc:
            assert str(exc)=='Exposure (us) value 1 rejected: ASISetControlValue: general error (ASI error 16)',exc
    finally:
        cam.close()
        os.environ.pop('FAKE_ASI_REJECT_CONTROL',None)


def device_loss(root):
    """USB loss during preview: one ERROR naming it, one logged line, then the process exits once.
    A camera error while the camera is still enumerated keeps the process (no false 'disappeared')."""
    log=root/'loss.log'
    os.environ['FAKE_ASI_REMOVE_AFTER']='2'
    try:
        cam=driver.ASICameraClient(log_path=log)
        assert cam.log_path==log
        try:
            cam.configure(2000,100,50,1,(0,0,64,32))
            for i in range(2):
                cam.expose(root/f'before_loss_{i}.npy')
            try:
                cam.expose(root/'after_loss.npy')
                raise AssertionError('exposure after USB loss accepted')
            except driver.CameraError as exc:
                message=str(exc)
            assert message==('ASIStartExposure: camera removed (ASI error 5); USB device disappeared '
                             '(SDK reports 0 cameras connected)'),message
            assert cam._proc.wait(5)==2 and not cam.is_running
            assert not (root/'after_loss.npy').exists()
        finally:
            cam.close()
    finally:
        os.environ.pop('FAKE_ASI_REMOVE_AFTER')
    text=log.read_text()
    assert text.count('USB device disappeared')==1 and 'EXPOSE failed' in text,text
    os.environ['FAKE_ASI_EXPOSE_ERROR']='1'
    try:
        cam=driver.ASICameraClient(log_path=log)
        try:
            cam.configure(2000,100,50,1,(0,0,64,32))
            try:
                cam.expose(root/'transient.npy')
                raise AssertionError('failed exposure accepted')
            except driver.CameraError as exc:
                assert 'disappeared' not in str(exc) and 'ASI error 16' in str(exc),exc
            assert cam.is_running
        finally:
            cam.close()
    finally:
        os.environ.pop('FAKE_ASI_EXPOSE_ERROR')
    print('USB loss: one named error, logged, single process exit; other errors keep the process OK')
    # Seen on the real ASI533MM Pro on a USB 2 path: "exposure failed", then every command answered
    # "camera closed (ASI error 4)" while the process stayed up. Now it ends once, at the failure.
    log=root/'closed.log'
    os.environ['FAKE_ASI_FAIL_CLOSE_AFTER']='1'
    try:
        cam=driver.ASICameraClient(log_path=log)
        try:
            cam.configure(2000,100,50,1,(0,0,64,32))
            cam.expose(root/'before_close.npy')
            try:
                cam.expose(root/'failed_close.npy')
                raise AssertionError('failed exposure accepted')
            except driver.CameraError as exc:
                assert str(exc)==('exposure failed; camera handle lost: SDK reports the camera closed '
                                  '(ASI error 4); reconnect'),exc
            assert cam._proc.wait(5)==3 and not (root/'failed_close.npy').exists()
        finally:
            cam.close()
    finally:
        os.environ.pop('FAKE_ASI_FAIL_CLOSE_AFTER')
    text=log.read_text()
    assert text.count('camera handle lost')==1 and 'EXPOSE failed: exposure failed' in text,text
    print('camera closed by the SDK after a failed exposure: one named error, single process exit OK')


if __name__=='__main__':main()
