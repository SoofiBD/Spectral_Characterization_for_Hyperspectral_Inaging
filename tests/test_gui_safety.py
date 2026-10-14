"""GUI ownership, provenance, recovery and asynchronous shutdown; simulated devices only."""
from pathlib import Path
import json
import sys
import tempfile
import time
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog
from gui.qt_bootstrap import prepare_qt
from gui.main_window import MainWindow
import gui.monochromator_panel as mono_module


def wait(app,condition,seconds=15):
    end=time.monotonic()+seconds
    while not condition():
        app.processEvents();time.sleep(.01)
        if time.monotonic()>end:raise AssertionError('GUI condition timed out')


def main():
    prepare_qt()
    app=QApplication.instance() or QApplication([])
    warnings=[]
    QMessageBox.warning=lambda *args,**kw: warnings.append(args[2])
    QMessageBox.critical=lambda *args,**kw: warnings.append(args[2])
    QMessageBox.information=lambda *args,**kw: None
    QMessageBox.question=lambda *args,**kw: QMessageBox.Yes
    with tempfile.TemporaryDirectory(prefix='spectral-gui-test-') as tmp:
        win=MainWindow(Path(tmp),simulate=True);win.show()
        win.mono.connect('SIMULATED');win.hdx.connect(simulate=True)
        mp,cp=win.mono_panel,win.char_panel
        cp.cam_box.setChecked(False);cp.hdx_box.setChecked(True)  # HDX-only scans in this test
        mp._update_state();win.hdx_panel._update_state()
        callbacks={};original=mono_module.run_task
        try:
            mono_module.run_task=lambda *args,**kw:callbacks.update(kw)
            mp._run('POS',win.mono.get_position)
            mp.set_external_busy(True)
            callbacks['on_finished']()
            assert not mp.goto_btn.isEnabled() and not mp.connect_btn.isEnabled()
        finally:
            mono_module.run_task=original;mp.set_external_busy(False)
        cp.start_spin.setValue(500);cp.stop_spin.setValue(510)
        cp.avg_spin.setValue(1);cp.dark_avg_spin.setValue(1);cp.int_spin.setValue(6);cp.settle_spin.setValue(0)
        cp.bwd_chk.setChecked(False)
        cp.start();wait(app,lambda:cp.worker is None and len(cp.rows)==2)
        run=cp.run_dir;csv=cp.last_csv
        original_info=json.loads((run/'manifest.json').read_text())
        assert original_info['parameters']['position_check']=={'mode':'simulation','confirmed':False}
        assert original_info['preposition']['target_nm']==480 and str(run) in cp.folder_lbl.text()
        cp.int_spin.setValue(123);cp.avg_spin.setValue(999)
        QFileDialog.getOpenFileName=lambda *args,**kw:(str(csv),'CSV')
        cp.load_csv()
        assert cp.params['integration_ms']==6 and cp.params['averages']==1
        assert cp.recorded_devices==original_info['devices']
        QFileDialog.getExistingDirectory=lambda *args,**kw:str(run)
        cp.load_run()
        assert len(cp.rows)==2 and cp.run_dir==run
        assert cp.generate_pdf(interactive=False).is_file()
        # A long settle is interruptible; close must not destroy a running worker.
        cp.settle_spin.setValue(30);cp.dark_chk.setChecked(False)
        cp.start();wait(app,lambda:'Settling' in cp.state_lbl.text())
        worker=cp.worker
        start=time.monotonic();win.close()
        assert win._closing
        wait(app,lambda:cp.worker is None and not win.mono.is_connected,5)
        assert not worker.isRunning() and time.monotonic()-start<5
        assert json.loads((cp.run_dir/'manifest.json').read_text())['status']=='aborted'
        assert not warnings,warnings
    print('ALL GUI SAFETY TESTS PASSED')


if __name__=='__main__':main()
