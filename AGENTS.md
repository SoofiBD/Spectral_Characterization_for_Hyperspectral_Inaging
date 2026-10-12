# Birleştirme görevi

Bu depo iki uygulamayı tek projede birleştirir:

- Kök dizin: Python 3.11+/PySide6 monokromatör + Ocean HDX uygulaması (`devices/`, `gui/`, `analysis/`, `tests/`, `main.py`). **Temel bu.**
- `hsi/lamda_hsi/`: C++17/GLFW/ImGui + ZWO ASI533MM Pro (ASI SDK v1.41) uygulaması. Yalnızca kamera erişimi buradan alınır.

## Hedef mimari

- Tek tarama koordinatörü (Python) monokromatörün tek sahibidir. C++ tarafı seri portu açmaz.
- Kamera: C++ ayrı yerel süreç (ASI SDK), yerel protokolle komut alır (ayarları oku, pozla, iptal, durum). Tam çözünürlüklü RAW16 kareyi oturum dizinine kendisi yazar, koordinatöre yalnızca yol + özet bildirir.
- Her nokta: GOTO → DONE + bildirilen konumu doğrula (süre sınırı zorunlu) → settle → HDX ham spektrumları + kamera RAW16 → ikisi de diske yazılıp özeti alınınca nokta tamam.
- Edinim önce ardışık; eşzamanlı pozlama sonra.
- Durumlar: Idle → Preparing → Moving → Settling → Acquiring → Saving → Complete; iptal/süre aşımı/bağlantı kaybı → Stopping/Failed. Yazılmış noktalar korunur; yeniden açılışta motor konumu sorgulanmadan otomatik devam yok.

## Veri kuralları

- Ham veri (HDX `.npz`, RAW16) asla değiştirilmez; düzeltme, tepe/FWHM, küp, CSV/PDF/ENVI ham dosyadan yeniden üretilebilir.
- Oturum manifesti (sürümlü JSON): aygıt seri no + sürücü/SDK sürümü, UTC zamanlar, hedef/bildirilen dalga boyu, yön, settle, HDX entegrasyonu, kamera pozlama/gain/offset/ROI/binning/format/sıcaklık, dark/white kimliği + SHA-256, her ham dosyanın boyutu + SHA-256, analiz sürümü + ayarları.
- Dark/white ayrı ölçüm. Pozlama/gain/offset/ROI/binning/format eşleşmeyen referans **uygulanmaz** (yalnızca piksel sayısı yetmez).
- Dosya yolları oturum köküne göreli; kullanıcı girdisi dosya adına güvenli hale getirilmeden girmez.

## Çalışma sırası

1. Değişiklik yapmadan iki kod tabanını incele (`hsi/hsi_performance_audit.md` dahil), dosya planını raporla, onay bekle.
2. Simüle mono/HDX/kamera ile **tek noktalık** uçtan uca edinim + manifest + test.
3. Testler: hareket süre aşımı, iptal, kalibrasyon uyuşmazlığı, yarım kalmış koşu.
4. Sonra tam tarama, sonra analiz/dışa aktarım.

## Kısıtlar

- Gerçek donanımı açık kullanıcı onayı olmadan hareket ettirme/pozlama başlatma.
- C++ derlenemiyorsa simüle kamera sürecini Python'da yaz; C++ tarafını protokol sınırına kadar hazırla, derlenmediğini raporla.
- Yeni bağımlılık ekleme gerekçesiz. Mevcut `devices/`, `analysis/`, `gui/` yapısını koru.
- `data/` eski ölçümler içerir, git dışında; gerekirse yeniden ölçülecek.
- Her adım sonunda: yapılan değişiklikler + çalıştırılan testler + çalıştırılamayanlar.
