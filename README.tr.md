# ig-publish (Türkçe özet)

Instagram hikâyelerini ve Reels'i, bilgisayarınızdaki dosyalardan, resmî Graph API üzerinden yayımlayan bir komut
satırı aracı ve Python kütüphanesi. Dosyaları herkese açık bir yere koymanız gerekmez: video doğrudan Meta'nın
yükleme sunucusuna (rupload) gider.

Asıl derdi güvenlik: hiçbir gönderiyi iki kez yayımlamaz, hesabınızın toplu paylaşım yüzünden kısıtlanma riskini
azaltır ve bir şey ters giderse ne olduğunu tahmin etmek zorunda bırakmaz.

## Neler yapar

- **Önce plan, sonra yayın.** Her komut `--apply` verilmedikçe yalnızca ne yapacağını gösterir.
- **`prep`**, videoları ffmpeg ile Instagram'ın sorunsuz kabul ettiği biçime çevirir ve sonucu ayrıca denetler.
  Hikâyeler 3-60 saniye olmalıdır; durağan bir görselden kısa bir video hikâye de yapılabilir.
- Bir çalıştırmadaki **bütün konteynerler yüklenip hazır olmadan** hiçbir şey yayımlanmaz; sonra her şey
  manifestteki sırayla çıkar.
- Bir hata gelince önce Instagram'a "gerçekte ne oldu" diye sorulur: gönderi çıkmışsa kaydedilir, **asla yeniden
  yayımlanmaz.**
- Spam, paylaşım sınırı ve istek sınırı (rate limit) hatalarında durur ve bekler; arka arkaya çok gönderiye, Reels
  aralığına, kotaya ve hesapta elle atılmış gönderilere bakar.
- Açıklama (caption) denetimleri: yasaklı kelimeler, lisanslı şarkı adları, zorunlu metin, sezona özel kelimeler.
- Zamanlayıcı (`ig-publish schedule run --apply`): dosyadaki saatlerde yayımlar, geçici durmalarda tekrar dener,
  uyuyan bir makine uyanınca toplu paylaşım yapmaz, size haber verir.
- Erişim anahtarı (token) ortam değişkeninden, yalnızca sizin okuyabildiğiniz bir dosyadan, macOS Anahtar
  Zinciri'nden ya da AWS SSM'den okunur; komut satırına, adrese, kayıtlara ya da dosyalara hiç yazılmaz.
- Docker imajı ve AWS EC2 kurulum rehberi. Python 3.10+; 3.11 ve sonrasında ek bağımlılık yok (3.10'da TOML okumak
  için gereken küçük `tomli` paketi kendiliğinden kurulur).

## Kurulum (yaklaşık 15 dakika)

Tıklanacak her yeri tek tek gösteren rehber: [docs/setup.md](docs/setup.md) (İngilizce; adım numaraları
oradakilerle aynı). Meta menülerinin adları sık değişiyor, ekranınızda biraz farklı yazabilir.

1. **Kurun.** pipx yoksa önce `brew install pipx && pipx ensurepath` (macOS) ya da
   `sudo apt install pipx && pipx ensurepath` (Debian, Ubuntu) çalıştırıp yeni bir terminal açın.

   ```bash
   pipx install "git+https://github.com/deegitech/ig-publish.git"
   mkdir benim-paylasimlarim && cd benim-paylasimlarim
   ig-publish init      # ig-publish.toml, manifest.json ve schedule.txt dosyalarını yazar
   ```

2. **Hesabı hazırlayın.** Instagram hesabınız *İşletme* (Business) türünde ve bir Facebook sayfasına bağlı olmalı;
   Facebook hesabınızın da o sayfada tam yetkisi ya da içerik yetkisi bulunmalı. *İçerik üreticisi* (Creator)
   hesaplar API ile hikâye paylaşamıyor (Ekim 2026'da gözlemledik). Hikâye arşivini açık bırakın: API ile paylaşılan
   hikâyeleri 24 saatten sonra öne çıkanlara yalnız arşivden ekleyebilirsiniz.
3. **Bir Meta uygulaması oluşturun.** developers.facebook.com → My Apps → Create App; kullanım alanı olarak *Manage
   messaging & content on Instagram*. Panelde Use cases → Customize → *API setup with Facebook login* bölümünden şu
   izinleri ekleyin: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`
   (gönderi silmek için ayrıca `instagram_manage_contents`). Uygulama *Development* modunda kalabilir; App Review
   gerekmez.
4. **Erişim anahtarını (token) alın.** Graph API Explorer'da uygulamanızı seçin, izinleri ekleyin ve *Generate Access
   Token*'a basın. Açılan pencerede **hem Facebook sayfanızı hem Instagram hesabınızı** işaretleyin; en sık yapılan
   hata yalnızca birini işaretlemek. Ardından anahtarın yanındaki (i) → Access Token Debugger → *Extend Access Token*:
   yaklaşık 60 gün geçerli uzun ömürlü anahtar budur.
5. **Hesap kimliğini bulun.** Explorer'da `me/accounts?fields=name,instagram_business_account` sorgusunu çalıştırın.
   `instagram_business_account.id` değeri, tırnak içinde, `ig-publish.toml` içindeki `ig_user_id` olur; @kullanıcı
   adı ya da sayfa kimliği değil. `init`'in yazdığı `[account]` bölümündeki satırı düzeltin, ikinci bir `[account]`
   başlığı eklemeyin.
6. **Anahtarı güvenle saklayın.** Anahtarı hiçbir dosyaya, sohbete ya da komut satırına yazmayın. Mac'te Anahtar
   Zinciri'ni kullanın: aşağıdaki satırı yazın ama Enter'a basmayın, anahtarı kopyalayın, sonra Enter'a basın ve
   panoyu `pbcopy </dev/null` ile temizleyin.

   ```bash
   security add-generic-password -U -a "$USER" -s ig-publish -w "$(pbpaste)"
   ```

   `-w`'den sonraki değeri boş bırakmayın: o zaman çıkan parola istemi yapıştırılanı sessizce 128 karakterde
   kesiyor (Ekim 2026'da gözlemledik), Meta anahtarları ise 200 karakter kadar. Komutla anahtarı tek seferde
   yapıştırmayın, yoksa anahtarın yerine komut metni kaydedilir. Ardından `ig-publish.toml` içindeki mevcut
   `[token]` bölümüne `source = "keychain"` ve `keychain_service = "ig-publish"` yazın. Sunucuda yalnız sizin
   okuyabildiğiniz (0600) bir dosya ya da AWS SSM kullanın.
7. **Kontrol edin.** Önce `manifest.json`'daki üç örneği kendi dosyalarınızla değiştirin: `init`'in koyduğu
   örneklerin dosyaları yok, `doctor` bunu ✗ ile gösterir. Sonra `ig-publish doctor` çalıştırın: kurulumu sırayla
   denetler (ayar dosyası, manifest, FFmpeg, anahtar, izinler, hesap, sayfa bağlantısı, kota), hiçbir şey yazmaz,
   anahtarı asla göstermez ve her ✗ satırının altına ne yapmanız gerektiğini yazar.
8. **İlk çalıştırma:** önce `ig-publish prep && ig-publish plan`, sonra `ig-publish publish --canary --apply`.

Anahtar yaklaşık 60 günde bir yenilenmeli: takviminize hatırlatıcı koyun, süresi dolmadan 4. ve 6. adımları
tekrarlayın, ardından `ig-publish doctor` çalıştırın. Hata kodlarının anlamı ve çözümleri:
[docs/troubleshooting.md](docs/troubleshooting.md). Araç da bilinen her hatanın altına bir `fix:` satırı ekler.

## Hızlı başlangıç

```bash
pipx install "git+https://github.com/deegitech/ig-publish.git"
mkdir benim-paylasimlarim && cd benim-paylasimlarim
ig-publish init                          # ayar dosyası, manifest ve zamanlama örneği
$EDITOR ig-publish.toml manifest.json    # ig_user_id ve dosyalarınız
read -rs IG_ACCESS_TOKEN && export IG_ACCESS_TOKEN   # ya da Anahtar Zinciri, 0600 dosya, AWS SSM
ig-publish doctor && ig-publish prep && ig-publish plan
ig-publish publish --canary --apply      # önce yalnız ilk gönderi
ig-publish publish --apply
```

Hesaba uygulamadan elle bir gönderi attıysanız araç, yeni bir şey yayımlamadan önce durup sorar.
`ig-publish ack --apply` bu gönderileri kayda geçirir; zamanlayıcı bir sonraki denemede kaldığı yerden devam eder.

API'nin yapamadıkları da açıkça yazılı: çıkartma, bağlantı, Instagram müzik kitaplığı ve öne çıkanlar API ile
eklenemez. Ayrıntılar, ayar başvurusu ve güvenlik modeli için [README.md](README.md) (İngilizce).

## Hakkında

ig-publish'i DEEGITECH Teknoloji ve Yazılım Ltd. Şti. olarak, kendi iOS oyunumuzun tanıtımını yaparken yazdık ve
hâlâ kullanıyoruz. MIT lisansıyla açık kaynak; hata bildirimlerini ve katkıları memnuniyetle karşılıyoruz.
