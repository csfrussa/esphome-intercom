# ESP Audio Stack Refactor Plan

This branch mutates `esp_audio_stack` directly toward an Espressif-native
backend. It intentionally does not keep a selectable legacy conversion path:
when an imported Espressif primitive fails, the audio session stops and reports
an error. The target is still to preserve ESPHome's microphone, speaker, media
player and mixer APIs while moving lower audio transport and conversion work to
Espressif components wherever they fit.

Audit update 2026-05-20: `esp_board_manager/periph_i2s` was checked against the
official source and is not the active bus owner in this branch anymore. It is an
official Espressif adapter, but it hides `i2s_chan_config_t` DMA policy and
enables channels during ref. The active implementation uses the official IDF
`esp_driver_i2s` layer directly for channel ownership, then hands those handles
to `esp_codec_dev` and GMF IO. This is still Espressif-native, not a legacy
fallback.

## Dependency Policy

Prototype builds track the ESP Component Registry latest version by default.
Do not add a `ref` for Espressif components unless a concrete upstream
regression is reproduced and documented in this repository.

Observed registry latest on 2026-05-20:

| Component | Registry latest | Why it matters |
|---|---:|---|
| `espressif/esp_audio_effects` | `1.3.0~1` | Standalone effects: rate, bit, channel, data weaving, ALC, mixer, DRC, MBC, howling suppression. |
| `espressif/esp_codec_dev` | `1.5.10` | Codec control and `esp_codec_dev_read/write` over IDF I2S handles. |
| `espressif/esp-sr` | `2.4.4` | AEC, AFE, VAD, BSS/SE, AGC and current full-duplex AEC generation. |
| `espressif/gmf_ai_audio` | `0.8.3` | GMF AFE manager, GMF AEC element, WakeNet/AFE elements. |
| `espressif/gmf_audio` | `0.8.3` | Audited future GMF audio processing elements: rate, bit, channel conversion, interleave/deinterleave, mixer, effects. Not an active runtime dependency in the current branch. |
| `espressif/gmf_io` | `0.8.1` | GMF IO streams, including `io_codec_dev`. |
| `espressif/esp-dsp` | `1.8.2` | Audited but not an active runtime dependency after moving hot audio gain/conversion paths to `esp-audio-libs` and `esp_audio_effects`. |

Sources: ESP Component Registry pages for
[`esp_audio_effects`](https://components.espressif.com/components/espressif/esp_audio_effects),
[`esp_codec_dev`](https://components.espressif.com/components/espressif/esp_codec_dev),
[`esp-sr`](https://components.espressif.com/components/espressif/esp-sr),
[`gmf_ai_audio`](https://components.espressif.com/components/espressif/gmf_ai_audio),
[`gmf_audio`](https://components.espressif.com/components/espressif/gmf_audio), and
[`gmf_io`](https://components.espressif.com/components/espressif/gmf_io).

## Hard Boundaries

Keep these ESPHome-facing contracts stable:

| Contract | Keep ESPHome-facing? | Reason |
|---|---:|---|
| `microphone: platform: esp_audio_stack` | Yes | Voice Assistant, Micro Wake Word and intercom already depend on ESPHome microphone callbacks. |
| `speaker: platform: esp_audio_stack` | Yes | ESPHome media player, resampler and mixer write into this speaker abstraction. |
| ESPHome `mixer` speaker | Yes | It already provides source speakers, ducking and pending-playback accounting. GMF mixer would duplicate and complicate that behavior. |
| Mic consumer registry | Yes | ESPHome has no native concept of multiple downstream mic consumers sharing one hardware bus. |
| Speaker lifecycle hooks | Yes | Board packages use `on_speaker_start` and `on_speaker_idle` for PA/power policy. |
| Post-processor fanout | Yes | MWW, VA and intercom must receive the same post-AEC/AFE stream. |
| Home Assistant entities | Yes | Switches, numbers, sensors and YAML validation are ESPHome concerns. |

Everything below those contracts is eligible for Espressif-native implementation.

## Obiettivo Del Porting

Il target non e' "aggiungere GMF" e basta. Il target e' portare
`esp_audio_stack` su uno stack Espressif end-to-end, dal bus I2S full-duplex
fino ai consumer ESPHome:

```text
ESPHome YAML
  -> ESPHome compatibility layer: microphone, speaker, mixer, automations, sensors
  -> Espressif lifecycle bridge: start/stop/suspend based on ESPHome consumers
  -> GMF/ESP audio pipeline: IO, rate/bit/channel conversion, interleave, AEC/AFE
  -> esp_codec_dev
  -> esp_driver_i2s
  -> IDF I2S full-duplex hardware
```

Regola di progetto:

| Codice | Ammesso? | Motivo |
|---|---:|---|
| Glue ESPHome per YAML, classi `microphone`/`speaker`, mixer, automazioni, sensori | Si | E' la compatibilita richiesta dal porting. |
| Policy board non rappresentabile in componenti Espressif | Si, ma minima | Serve per board custom ESPHome. |
| DSP manuale duplicato da `esp_audio_effects`/`gmf_audio` | No | Deve passare a primitive/elementi Espressif. |
| Read/write codec custom se `gmf_io/io_codec_dev` funziona | No | Deve diventare IO Espressif. |
| Fallback legacy nascosto | No | Maschera rotture e invalida il test del porting. |

## Stack I2S Ufficiale Espressif

La gestione I2S ufficiale Espressif non e' un solo componente GMF. E' uno
stack a livelli:

| Layer Espressif | Cosa gestisce | Cosa non gestisce | Stato per noi |
|---|---|---|---|
| IDF `esp_driver_i2s` / `driver/i2s_std.h` / `driver/i2s_tdm.h` | Canali RX/TX, DMA, clock, STD/TDM/PDM, `i2s_channel_read/write`, enable/disable | Codec esterni, policy board, consumer ESPHome, mixer ESPHome | Owner bus attivo. Configura DMA, STD/TDM e full-duplex direttamente con API ufficiali IDF. |
| `esp_board_manager` `periph_i2s` | Configurazione board-driven di I2S, refcount periferica, handle condivisi, STD/TDM/PDM | YAML ESPHome runtime, callback mic/speaker, DMA policy esposta | Audited, non attivo: troppo poco controllo su DMA/lifecycle per questo porting ESPHome. |
| `esp_codec_dev` | Device codec sopra data interface I2S, open/read/write, gain/volume/mute | Pipeline, AFE, mixer, fanout consumer | Gia usato. E' il ponte codec corretto. |
| `gmf_io` `io_codec_dev` | Reader/writer GMF sopra `esp_codec_dev_handle_t`, buffer IO, task, speed monitor | Creazione pin/slot I2S, scelta board, semantica ESPHome speaker/mic | Candidato immediato per sostituire `esp_codec_dev_read/write` nel task. |
| `gmf_app_utils` setup codec | Helper example-level: setup codec/I2S/I2C e handle playback/record | Solo playback STD dichiarato nel struct, troppo opinionated per ESPHome | Reference utile, non base diretta del componente. |
| GMF `io_i2s_pdm` | IO GMF per PDM | Full duplex STD/TDM codec path | Fuori target ora. |

Quindi: si', Espressif ha librerie native I2S. La frase corretta non e'
"non esiste un sostituto Espressif", ma: GMF da solo non sostituisce tutta la
nostra config I2S/board policy. Il candidato attivo per quella parte e'
`esp_driver_i2s`; il candidato per il read/write audio e'
`gmf_io/io_codec_dev`.

## Comparazione: `periph_i2s` Vs `esp_driver_i2s`

Questa non e' una scelta tra "ufficiale" e "custom". Entrambi sono ufficiali
Espressif: `periph_i2s` e' un adapter di `esp_board_manager` che usa sotto il
driver IDF `esp_driver_i2s`.

| Tema | `esp_driver_i2s` diretto | `esp_board_manager/periph_i2s` | Impatto sul porting |
|---|---|---|---|
| Natura | Driver IDF hardware: `i2s_new_channel`, `i2s_channel_init_std_mode`, `i2s_channel_init_tdm_mode`, enable/disable/read/write | Adapter board-level sopra il driver IDF | Non e' fallback legacy usare il driver diretto: e' il layer ufficiale piu basso. |
| Ownership canali | La nostra classe possiede handle IDF ufficiali `tx_handle_` e `rx_handle_` | `periph_i2s` possiede handle globali per porta in `i2s_chan_handles[SOC_I2S_NUM]` | Il branch attivo resta sul driver diretto; con `periph_i2s` perderemmo controllo puntuale di delete/lifecycle. |
| Config ingresso | Struct create da C++ a runtime da YAML ESPHome | `periph_i2s_config_t` da board manager/generated config, o chiamata diretta a `periph_i2s_init` | Possibili due approcci: full board manager o chiamata diretta all'adapter. |
| Full duplex | Creiamo TX/RX insieme con `i2s_new_channel(&cfg, &tx, &rx)` su single bus | Crea TX/RX insieme quando la porta e' vuota | Allineato. |
| Clock TX per RX | Il branch crea TX anche per clock full-duplex, anche mic-only con `dout = GPIO_NUM_NC` | `periph_i2s` inizializza/abilita TX se viene richiesta RX e TX non e' attivo | Allineato come semantica clock; cambia solo chi possiede il lifecycle. |
| STD mode | Controllo completo di TX/RX config separate: TX mono/stereo, RX mono/stereo/ref | Supporta STD con `i2s_std_config_t` | Da verificare bene se accetta TX/RX config diverse sulla stessa porta come ci serve per ES8311 stereo ref. |
| TDM mode | Controllo completo di `total_slot`, mask, slot width, slot policy | Supporta TDM con `i2s_tdm_config_t` | Probabile compatibile; va validato su ES7210/ES8311 TDM. |
| PDM | Non target attuale del duplex codec | Supporta PDM TX/RX dove SoC lo supporta | Utile futuro, non blocca il porting attuale. |
| DMA | Noi calcoliamo `dma_frame_num` per ~10 ms e limite 4092 byte | `periph_i2s` usa `I2S_CHANNEL_DEFAULT_CONFIG`, quindi non espone nel suo config YAML base gli stessi calcoli | Punto critico: se perdiamo controllo DMA/latency, meglio driver diretto o patch adapter. |
| Auto clear | Noi usiamo `auto_clear_after_cb` e campi IDF 5.2 | `periph_i2s` imposta `chan_cfg.auto_clear = true` | Da confrontare su versione IDF usata da ESPHome. |
| Enable timing | Il lifecycle ESPHome separa prepare READY da open RUNNING | `periph_i2s_init` inizializza e abilita subito | Motivo dello scarto: il driver diretto ci lascia preparare/init e far abilitare a `esp_codec_dev_open()`. |
| Refcount | Nostro lifecycle interno | `esp_board_periph_ref_handle/unref_handle` ha refcount per nome | Utile se codec/input/output condividono la stessa periferica. |
| Deinit | Noi disabilitiamo codec, poi delete TX/RX | `periph_i2s_deinit` disabilita/delete per handle e puo' forzare lo stop del pair | Va gestito con attenzione per non spegnere clock mentre RX/TX serve ancora. |
| Dual bus no-codec | Nostro codice crea due porte separate TX/RX | `periph_i2s` puo' inizializzare periferiche distinte per nome/porta | Compatibile in teoria; serve mapping YAML -> due config `periph_i2s_config_t`. |
| Codec handoff | Passiamo handle a `audio_codec_new_i2s_data` nel nostro `CodecDevBackend` | `dev_audio_codec` Espressif usa `esp_board_periph_ref_handle`, poi `audio_codec_new_i2s_data` | Pattern Espressif molto vicino al nostro backend codec. |
| Runtime YAML ESPHome | Naturale: generiamo C++ direttamente dalla config utente | Meno naturale se usa database statico board manager | Non dobbiamo forzare board database se complica ESPHome. |
| Knob ufficiali | Tutti i campi IDF esponibili direttamente | Solo campi previsti da `periph_i2s_config_t`/parser | Driver diretto espone piu' controllo; adapter espone ownership/refcount. |

Decisione aggiornata:

| Opzione | Quando usarla | Quando scartarla |
|---|---|---|
| `esp_board_periph_ref_handle()` full board manager | Riferimento ufficiale per board statiche e BSP | Scartato come backend attivo finche non espone DMA/lifecycle richiesti. |
| `periph_i2s` diretto con `periph_i2s_init()` | Strumento intermedio solo se serve isolare un problema dell'adapter I2S durante il porting | Non deve diventare backend pubblico o fallback. |
| `esp_driver_i2s` diretto | Backend runtime attivo: API ufficiale IDF, controllo completo di DMA, STD/TDM e dual-bus | Non deve diventare doppio path parallelo: e' l'unico owner bus nel refactor attuale. |

### Mapping YAML I2S Verso Espressif

| YAML attuale | `esp_driver_i2s` | `periph_i2s_config_t` | Rischio |
|---|---|---|---|
| `i2s_num` | `i2s_chan_config_t.id` | `port` | Nessuno. |
| `i2s_mode: primary/secondary` | `i2s_chan_config_t.role` master/slave | `role` | Nessuno. |
| `sample_rate` | `clk_cfg.sample_rate_hz` | `i2s_cfg.std/tdm/pdm*.clk_cfg.sample_rate_hz` | Nessuno. |
| `bits_per_sample` | `slot_cfg.data_bit_width` | `slot_cfg.data_bit_width` | 24-bit usa container DMA 32-bit: va mantenuta la stessa policy. |
| `slot_bit_width` | `slot_cfg.slot_bit_width` | `slot_cfg.slot_bit_width` | Nessuno se mappato prima di init. |
| `mclk_multiple` | `clk_cfg.mclk_multiple` | `clk_cfg.mclk_multiple` | Nessuno. |
| `use_apll` | `clk_cfg.clk_src` se SoC supporta APLL | `clk_cfg.clk_src` | Board manager parser espone clock source, ma il nostro boolean va tradotto bene. |
| `i2s_comm_fmt` | helper STD/TDM Philips/MSB/PCM short/long | config STD/TDM gia espone bit shift/align/polarity | PCM short/long sono TDM-only: validazione nostra resta. |
| `i2s_mclk_pin` | `gpio_cfg.mclk` | `gpio_cfg.mclk` | Nessuno. |
| `i2s_bclk_pin` | `gpio_cfg.bclk` | `gpio_cfg.bclk` | Nessuno. |
| `i2s_lrclk_pin` | `gpio_cfg.ws` | `gpio_cfg.ws` | Nessuno. |
| `i2s_din_pin` | `gpio_cfg.din` | `gpio_cfg.din` | Nessuno. |
| `i2s_dout_pin` | `gpio_cfg.dout` | `gpio_cfg.dout` | Nessuno. |
| `num_channels` | TX `slot_mode`/`slot_mask` | STD slot config | Da verificare TX mono + RX stereo nella stessa porta. |
| `mic_channel` | RX mono `slot_mask` | STD slot config | Da verificare se `periph_i2s` consente TX/RX config diverse quando inizializzati separatamente. |
| `tx_channel` | TX mono `slot_mask` | STD slot config | Stesso rischio sopra. |
| `use_stereo_aec_reference` | RX forzato stereo, TX indipendente | RX STD stereo config | Caso critico ES8311: serve RX stereo anche se TX mono. |
| `use_tdm_reference` | `i2s_tdm_config_t` + slot policy | `i2s_tdm_config_t` | Probabile ok. |
| `tdm_total_slots` | TDM mask + `total_slot` derivato | `slot_cfg.total_slot`/mask | Va preservata la nostra mask 0..N-1. |
| `tdm_mic_slot(s)` | Post-deinterleave policy | Non e' bus config | Resta glue ESPHome/pipeline. |
| `tdm_ref_slot` | Post-deinterleave policy | Non e' bus config | Resta glue ESPHome/pipeline. |
| `rx_bus`/`tx_bus` | Due `i2s_new_channel` separati | Due `periph_i2s_config_t` su nomi/porte diverse | Va testato per no-codec dual-bus. |
| DMA duration / latency implicita | Calcolo nostro `dma_frame_num` | Non esposto dal parser base; `periph_i2s_init` usa default config | Rischio maggiore per AEC. |
| `buffers_in_psram` | Buffer nostri, non I2S driver | Non bus config | Da spostare sui buffer GMF/IO quando cablati. |

### Reference AEC Senza Codec

Espressif distingue due problemi che noi avevamo mischiato nel componente:
trasporto audio senza codec e reference AEC. Il primo e' coperto dal loro stack:
IDF espone il driver `esp_driver_i2s` per STD/TDM/PDM, `esp_board_manager`
espone `periph_i2s`, e GMF IO espone almeno `io_i2s_pdm` oltre a
`io_codec_dev`. Quindi un device senza codec e' assolutamente supportabile lato
bus/IO.

Il secondo problema, pero', non viene risolto automaticamente dal bus. AEC vuole
sempre una reference coerente. Gli esempi Espressif GMF AEC ufficiali partono da
board audio con reference hardware o loopback gia presente nel capture stream.
L'esempio `aec_rec` usa due pipeline:

```text
playback: File -> Decoder -> Effects -> CODEC_DEV_TX
record:   CODEC_DEV_RX -> Rate_cvt -> AEC -> File/Encoder
```

L'elemento AEC riceve un formato come `RMNM`: `R` e' il reference/loopback,
`M` sono i microfoni, `N` sono canali ignorati. Quindi Espressif, nei progetti
codec, non deve inventarsi una reference software dal frame TX precedente: la
reference arriva dal codec/ADC/I2S RX.

Quando non c'e' un codec o un ADC che rimanda il playback nel capture stream,
Espressif non ha un "previous frame" magico nel driver I2S: il modello ufficiale
e' che l'applicazione fornisca `mic` e `ref` all'AEC diretto, oppure costruisca
un frame interleaved con canale `R` per AFE/GMF AEC. Per i nostri device senza
codec questa feature va quindi mantenuta, ma spostata nel bridge ESPHome/GMF:

| Topologia | Reference Espressif naturale | Nostro requisito | Porting |
|---|---|---|---|
| ES8311 stereo loopback | Canale RX stereo con mic + DAC ref | `use_stereo_aec_reference` | GMF RX deinterleave -> formato `MR`/`RM` per AEC. |
| ES7210/ES8311 TDM | Slot TDM dedicato con DAC analog ref | `use_tdm_reference`, slot health monitor | GMF RX deinterleave/sort -> `MR`/`MMR`/`RMNM`. |
| I2S/PDM mic senza codec, speaker I2S separato | Bus e IO ufficiali, ma nessun loopback AEC automatico | `aec_reference: previous_frame` e `ring_buffer` | Bridge TX->AEC reference: il PCM post-volume speaker diventa il canale `R` della pipeline AEC. |

Quindi `previous_frame` non e' una libreria Espressif da importare e non deve
restare come DSP parallelo. E' il glue di prodotto necessario per dare a
Espressif AEC il canale `R` quando l'hardware non lo fornisce. Nel porting deve
diventare un bridge di formato/reference nella pipeline: stesso PCM che esce
dallo speaker ESPHome, stesso clock-domain policy del bus, payload interleaved
verso AFE/AEC.

### Metodo Ufficiale Espressif Per Reference Software

Il riferimento piu vicino al nostro caso non e' nell'esempio GMF `aec_rec`, ma
nel vecchio ESP-ADF ufficiale: `algorithm_stream`.

`algorithm_stream` dichiara due metodi:

| Metodo Espressif | Significato | Equivalente per noi |
|---|---|---|
| `ALGORITHM_STREAM_INPUT_TYPE1` | Mic e reference arrivano gia nello stesso I2S, canali L/R o slot equivalenti. | Codec stereo loopback o TDM reference hardware. |
| `ALGORITHM_STREAM_INPUT_TYPE2` | Il record arriva dal reader I2S, la reference arriva da un secondo input ring buffer scritto dal playback path. | No-codec `previous_frame`/`ring_buffer`, ma realizzato come bridge ufficiale-style verso AEC. |

Nel loro esempio `advanced_examples/algorithm`, quando `RECORD_HARDWARE_AEC` e'
disabilitato:

```text
playback path -> i2s_write_cb -> rb_write(ringbuf_ref, playback_pcm)
record path   -> i2s_read_cb  -> algorithm_stream
algorithm_stream TYPE2:
  read reference da multi-input ringbuf
  read record da input I2S
  interleave record/ref
  feed AFE/AEC
```

Questo conferma che la reference software non e' una nostra invenzione: e' un
pattern ufficiale Espressif quando l'hardware non fornisce il canale `R`.
La parte da non copiare pari pari e' ADF/audio_pipeline, perche' nel nostro
branch il target e' GMF/ESPHome. La parte da copiare come architettura e':
playback PCM post-volume/post-mixer scritto in un reference ring, delay
configurabile, record+ref interleaved secondo `MR`/`RM`, AEC Espressif come
unico processore.

## Piano Operativo End-To-End

### Fase 0: Owner I2S Ufficiale

| Step | Azione | Esito atteso |
|---|---|---|
| 0.1 | Auditare `esp_board_manager/periph_i2s` contro `esp_driver_i2s` | Fatto: adapter ufficiale ma non abbastanza configurabile per DMA/lifecycle. |
| 0.2 | Usare `esp_driver_i2s` come owner unico del bus | Canali TX/RX ufficiali IDF, nessun backend parallelo. |
| 0.3 | Mappare ogni istanza `esp_audio_stack` a canali deterministici: single-bus, dual-bus RX, dual-bus TX | La classe possiede handle ufficiali IDF e li passa a `esp_codec_dev`. |
| 0.4 | Mantenere `prepare_i2s_channels_()`/`deinit_i2s_()` su API IDF ufficiali | Niente board-manager runtime e niente codice I2S example-local. |
| 0.5 | Adattare il lifecycle: `esp_codec_dev_open/close` governa enable/disable e GMF IO governa read/write | ESPHome mantiene consumer semantics senza read/write custom. |
| 0.6 | Se emergono limiti su DMA o TX/RX asimmetrici, esporre i knob IDF reali o correggere il mapping | Niente fallback legacy: si aggiusta il porting o si documenta la feature bloccante. |

Stato codice attuale:

| Step | Stato |
|---|---|
| Import `esp_board_manager` | Rimosso dal backend attivo dopo audit. |
| `CONFIG_ESP_BOARD_PERIPH_I2S_SUPPORT` | Rimosso; non serve senza board-manager runtime. |
| Simboli `g_esp_board_info`, `g_esp_board_peripherals`, `g_esp_board_periph_handles`, device vuoti | Rimossi insieme a `board_manager_i2s_backend.*`. |
| `prepare_i2s_channels_()` su API ufficiali | Fatto: usa `i2s_new_channel`/`i2s_channel_init_std_mode`/`i2s_channel_init_tdm_mode` con DMA policy esplicita. |
| Deinit su API ufficiali | Fatto: chiude GMF IO/codec, poi `i2s_del_channel`. |
| `gmf_io/io_codec_dev` read/write | Fatto: RX/TX usano `esp_gmf_io_acquire/release_*` sopra `io_codec_dev`, senza path `esp_codec_dev_read/write` diretto nel nostro codice. |

### Fase 1: Porting Bus I2S

| Step | Azione | Esito atteso |
|---|---|---|
| 1.1 | Mappare tutti i campi YAML attuali su `i2s_chan_config_t`, `i2s_std_config_t` e `i2s_tdm_config_t` | Tabella completa: ogni knob attuale ha una destinazione IDF/Espressif o una ragione per restare glue ESPHome. |
| 1.2 | Confrontare il nostro `prepare_i2s_channels_()` con `esp_board_manager/periph_i2s.c` | Fatto: `periph_i2s` e' ufficiale ma meno compatibile con YAML runtime per DMA/lifecycle. |
| 1.3 | Usare `esp_driver_i2s` diretto come unico owner del bus | Meno wrapper, piu controllo sui knob ufficiali IDF realmente necessari. |
| 1.4 | Tenere `periph_i2s` come riferimento, non come fallback runtime | Restiamo comunque su libreria nativa IDF, non su codice parallelo. |

Decisione corrente: il branch usa `esp_driver_i2s` come owner unico del bus. Il
driver diretto non e' fallback legacy: e' l'API ufficiale IDF che conserva
controllo su DMA, full-duplex, TDM e dual-bus. `periph_i2s` resta riferimento
ufficiale utile, ma non path runtime.

### Fase 2: Porting Codec/IO

| Step | Azione | Esito atteso |
|---|---|---|
| 2.1 | Passare gli `esp_codec_dev_handle_t` RX/TX al backend GMF IO interno | Fatto: `CodecDevBackend` crea `audio_stack_rx`/`audio_stack_tx` come `io_codec_dev`, senza doppio owner. |
| 2.2 | Sostituire il read path `esp_codec_dev_read` nel nostro task con `gmf_io/io_codec_dev` reader | Fatto: `read()` chiama `esp_gmf_io_acquire_read`/`release_read`. |
| 2.3 | Sostituire il write path `esp_codec_dev_write` con `gmf_io/io_codec_dev` writer | Fatto: `write()` chiama `esp_gmf_io_acquire_write`/`release_write`. |
| 2.4 | Esporre knob ufficiali `io_size`, `buffer_size`, `enable_speed_monitor`, thread stack/prio/core/PSRAM | Fatto: YAML `gmf_io.reader` e `gmf_io.writer`. Default zero = sync IO ufficiale Espressif. |

### Fase 3: Porting Pipeline Audio

| Step | Azione | Esito atteso |
|---|---|---|
| 3.1 | Convertire `esp_ae_*` diretti in elementi `gmf_audio` dove GMF possiede gia il flusso | Pipeline allineata agli esempi IDF/GMF. |
| 3.2 | Portare bit conversion, rate conversion, channel conversion, interleave/deinterleave su elementi GMF | Niente DSP custom duplicato. |
| 3.3 | Tenere ESPHome mixer fuori da GMF mixer | Manteniamo ducking/source/pending playback gia offerti da ESPHome. |
| 3.4 | Usare ALC dove sostituisce boost utente; lasciare DRC/MBC/fade/EQ come feature opzionali future | Evita DSP custom nel gain senza introdurre tuning/latency non richiesti nel backend base. |

Decisione corrente: le conversioni sono gia su librerie ufficiali
`esp_audio_effects` (`esp_ae_rate_cvt`, bit conversion e data weaver), quindi
non c'e' DSP custom parallelo da mantenere. Il passaggio agli elementi
`gmf_audio` e' utile solo quando un'intera pipeline GMF possiede anche le bridge
ring verso ESPHome; farlo a meta aggiungerebbe solo latenza e copie.

### Fase 4: Consumer ESPHome

| Step | Azione | Esito atteso |
|---|---|---|
| 4.1 | Tradurre consumer registry ESPHome in start/stop/suspend pipeline Espressif | Il bus si ferma quando non ci sono mic/speaker consumer, ma la policy resta ESPHome. |
| 4.2 | Collegare output pipeline a fanout mic ESPHome | MWW, VA e intercom ricevono lo stesso stream come oggi. |
| 4.3 | Collegare input speaker/mixer ESPHome alla pipeline TX | Media player, mixer e ducking restano compatibili. |
| 4.4 | Mantenere automazioni `on_speaker_start`, `on_speaker_idle`, sensori e health monitor | Nessuna feature utente persa. |

### Fase 5: AEC/AFE

| Step | Azione | Esito atteso |
|---|---|---|
| 5.1 | Mappare reference `MR/MMR/RMNM` usando le stesse convenzioni degli esempi GMF AEC | Reference digitale, TDM e software arrivano nel formato atteso da Espressif. |
| 5.2 | Tenere `esp_gmf_afe_manager` per AFE full | Gia allineato a Espressif. |
| 5.3 | Valutare `esp_gmf_aec` per standalone AEC solo dopo GMF IO stabile | Evita di cambiare bus e AEC nello stesso passaggio. |
| 5.4 | Non importare WakeNet GMF nel primo porting | ESPHome usa MWW/TensorFlow, quindi sarebbe duplicazione. |

### Fase 6: Feature Parity

Prima di considerare il porting riuscito, devono restare disponibili:

| Feature attuale | Deve restare | Note |
|---|---:|---|
| Single-bus codec ES8311 stereo reference | Si | Ref digitale DAC/mic invariata. |
| ES7210/ES8311 TDM con mic slot e ref slot | Si | Slot policy YAML invariata. |
| No-codec dual-bus | Si | Anche se non primo target GMF, non va perso. |
| ESPHome mixer e ducking | Si | GMF mixer non sostituisce questa API. |
| MWW, VA, intercom sullo stesso stream post-processor | Si | Fanout ESPHome obbligatorio. |
| Speaker lifecycle e PA/power hooks | Si | Board package dipendono da questi hook. |
| Buffer PSRAM dove supportato dal layer reale | Si | Esporre knob ufficiali solo quando cablati. |

### Matrice Scenari Supportati

Questa e' la matrice di copertura del porting. "Coperto" significa che lo
scenario deve avere un percorso Espressif-first esplicito; non significa che
tutti gli scenari usino lo stesso identico componente Espressif.

| Scenario | Bus/IO Espressif | Reference AEC | Layer ESPHome da mantenere | Stato porting |
|---|---|---|---|---|
| Codec ES8311 single-bus, mic + speaker | `esp_driver_i2s` -> `esp_codec_dev` -> `gmf_io/io_codec_dev` | Stereo loopback RX se abilitato, altrimenti nessuna ref | Speaker API, mixer, volume, automazioni, PA hooks | Bus owner e GMF IO portati. |
| Codec ES8311 con stereo digital ref | `esp_driver_i2s` + `esp_codec_dev` RX/TX | Canale RX mic/ref, formato `MR`/`RM` per AEC | `reference_channel`, fanout mic, health | Layout gia su Espressif effects; GMF element pipeline da fare. |
| ES7210/ES8311 TDM con ref slot | `esp_driver_i2s` TDM + `esp_codec_dev` | Slot TDM reference, formato `MMR`/`RMNM` | YAML slot policy, slot level monitor, tdm health | TDM config passa a IDF driver; layout gia su Espressif effects. |
| No-codec I2S single-bus | direct `esp_driver_i2s` read/write | Software ref ufficiale-style ADF TYPE2: speaker PCM -> ref ring -> `MR`/`RM` | AEC delay policy, mixer/speaker ring, mic callbacks | Bus owner avviato; reference bridge resta glue di formato, non DSP parallelo. |
| No-codec I2S dual-bus | Due canali `esp_driver_i2s`, RX e TX su porte diverse | Software ref ring con delay configurabile; possibile drift handling/asrc se misurato | `rx_bus`/`tx_bus`, `aec_reference_buffer_ms`, PSRAM ref ring | Requisito da validare subito: non opzionale. |
| PDM mic senza codec | Driver IDF/GMF `io_i2s_pdm` dove supportato dal SoC | Nessuna ref automatica; serve software ref se c'e' speaker | Mic consumer ESPHome, eventuale speaker bridge | Non nel duplex attuale; candidato futuro separato, non blocca STD/TDM. |
| Mic-only consumer | RX `esp_driver_i2s`/codec IO | Nessuna ref se AEC non richiesto; ref disabilitata o silenzio esplicito | MWW/VA/intercom fanout, on_mic hooks | Deve restare supportato; TX clock-only solo quando serve clock full-duplex. |
| Speaker-only/media output | TX `esp_driver_i2s`/codec IO | Non applicabile | ESPHome speaker platform, mixer, ducking, on_speaker hooks | Deve restare supportato; GMF writer deve aggiornare pending/played frames. |
| Full AFE dual mic | Bus come sopra, layout `MMR`/`RMNM` | AFE manager Espressif riceve canali gia ordinati | Wake word ESPHome/TensorFlow, VA/intercom fanout | Gia allineato a `esp_gmf_afe_manager`; non importare WakeNet GMF ora. |
| Standalone AEC | Bus come sopra | `afe_aec` diretto oggi; possibile `esp_gmf_aec` dopo IO GMF | Processor API ESPHome | Non cambiare insieme al bus; valutare solo dopo GMF IO stabile. |

## Matrice Operativa Completa

Legenda: "nostro" non significa "duplicare Espressif"; significa solo codice
ESPHome necessario per YAML, callback, mixer, automazioni e board policy. Tutto
il resto va spostato su componenti Espressif o deve fallire esplicitamente.

### 1. Moduli Espressif

| Modulo | Cosa offre davvero | Stato nel branch | Dove entra oggi | Cosa manca | Knob YAML |
|---|---|---|---|---|---|
| `esp_codec_dev` | Codec ES7210/ES8311, volume/gain/mute e data-if I2S | Gia integrato | `CodecDevBackend` crea device RX/TX e apre sample format | Nessun read/write diretto nel nostro task: IO dati passa a GMF | `codec.input`, `codec.output`, gain, ref gain, `no_dac_ref`, `use_mclk` |
| `esp_audio_effects` | `rate_cvt`, `bit_cvt`, `ch_cvt`, data weaver, mixer, ALC, fade, EQ, DRC, MBC, sonic | Gia integrato e usato | RX/TX bit conversion, RX deinterleave, TX interleave, rate conversion, positive user `mic_gain` via ALC | DRC/MBC/EQ/howl restano feature opzionali: non servono al backend base e aggiungono tuning/latency | `audio_effects.rate_cvt_complexity`, `audio_effects.rate_cvt_perf_type` |
| `gmf_audio` | Elementi GMF `aud_rate_cvt`, `aud_bit_cvt`, `aud_ch_cvt`, `aud_intlv`, `aud_deintlv`, `aud_mixer`, effetti | Audited, non attivo | Per ora usiamo le primitive C sottostanti da `esp_audio_effects` | Convertire la catena diretta in pipeline GMF solo se l'intero grafo potra' possedere la data path senza rompere ESPHome | Futuri task/port buffer; mixer GMF non esposto ora |
| `gmf_io` | IO GMF, incluso `io_codec_dev` reader/writer sopra `esp_codec_dev_handle_t` | Integrato come owner RX/TX | `CodecDevBackend::read/write` usano `esp_gmf_io_acquire/release_*` | Prossimo salto: far possedere anche conversioni a elementi GMF quando il grafo sara' stabile | `gmf_io.reader.*`, `gmf_io.writer.*` |
| `gmf_ai_audio` | `esp_gmf_afe_manager`, `esp_gmf_aec`, WakeNet GMF, AFE GMF con state machine | Integrato per AFE | `esp_afe` usa `esp_gmf_afe_manager` e l'elemento ufficiale `esp_gmf_afe` con bridge ESPHome | Valutare `esp_gmf_aec` solo se semplifica davvero `esp_aec` standalone | AFE switches/sensors gia presenti; altri knob solo se cablati |
| `esp-sr` | AFE, AEC, VAD, NS, AGC, SE/BSS, modelli | Gia integrato via `esp_afe`/`esp_aec` | AFE manager e standalone AEC | Capire se `esp_gmf_aec` semplifica `esp_aec` | `esp_aec.mode`, `esp_afe` feature toggles |
| `esp-dsp` | Helper DSP generici | Non richiesto dal core audio attuale | Nessuno | Tenere fuori finche non serve una primitiva ufficiale specifica | Nessuno diretto |
| `esp_board_manager` / `periph_i2s` | Pattern ufficiali per board, refcount periferiche, init/deinit I2S STD/TDM/PDM | Audited, non attivo | Nessun backend runtime; resta riferimento ufficiale | Non espone abbastanza DMA/lifecycle per il nostro YAML ESPHome | Se Espressif espone nuovi knob, rivalutare senza aggiungere fallback paralleli |
| `esp_capture` | Pipeline capture high-level per audio/video | Non integrato | Nessuno | Non adatto ora: possiede lifecycle capture e collide con mic/speaker ESPHome | Nessuno |
| `esp_audio_render` | Render/player high-level, decoder, mixer, output codec | Non integrato | Nessuno | Non adatto ora: ESPHome media_player/mixer devono restare API pubbliche | Nessuno |
| `esp_asrc` | ASRC per domini clock diversi | Non integrato | Nessuno | Possibile candidato solo per dual-bus no-codec se emerge drift reale | Futuro, solo dopo misure |

### 2. Bus I2S E Lifecycle

| Blocco | Prima / codice nostro | Ora nel branch | Espressif importabile | Differenza concreta | Decisione |
|---|---|---|---|---|---|
| Allocazione canali I2S single-bus codec | `i2s_new_channel` + config STD/TDM in `esp_audio_stack` | Conservata su API ufficiale IDF, ripulita dal read/write diretto | `esp_driver_i2s` crea canali ufficiali e `esp_codec_dev` riceve gli handle | GMF IO non configura pin/slot: consuma un codec device gia aperto | Validare che il mapping runtime copra tutte le board codec |
| Clock full-duplex codec | TX/RX creati insieme; TX genera clock; enable/disable coordinati | Ora resta su `esp_driver_i2s`: TX clock-only anche per mic-only quando serve clock | Pattern Espressif codec/I2S conferma ordine TX/RX | `io_codec_dev` non risolve da solo l'ordine clock, va rispettato quando parte la pipeline | Testare start pipeline RX/TX senza race su codec reali |
| Read RX | Task chiamava `esp_codec_dev_read` | Ora `esp_gmf_io_acquire_read`/`release_read` su `io_codec_dev` | `gmf_io` `io_codec_dev` reader | Default sync mode mantiene la cadenza del nostro task; con buffer/task YAML si passa al data-bus GMF | Fatto, senza secondo path |
| Write TX | Task chiamava `esp_codec_dev_write` | Ora `esp_gmf_io_acquire_write`/`release_write` su `io_codec_dev` | `gmf_io` `io_codec_dev` writer | Default sync mode mantiene callback played-frame; async data-bus copia payload nel buffer GMF | Fatto, senza secondo path |
| Stop bus quando non serve | Consumer registry + speaker refcount fermano/avviano duplex | ESPHome decide lo stop; GMF IO/codec vengono chiusi e i canali IDF vengono cancellati | GMF `pipeline_stop`, task suspend, `esp_gmf_afe_manager_suspend` | Espressif non sa che MWW/VA/intercom sono consumer ESPHome; dobbiamo tradurre noi gli eventi | Lifecycle resta ESPHome-facing, dietro chiama close/suspend GMF |
| Speaker idle / pending playback | Callback speaker output alimenta mixer pending frames | Ancora callback ESPHome; TX handle arriva da board manager | Eventi GMF pipeline possono aiutare diagnostica, non sostituiscono ESPHome mixer | ESPHome mixer vuole sapere frame consumati dalla sua speaker API | Tenere callback pubblica; aggiornarla da GMF writer quando `io_codec_dev` diventa owner |
| Dual-bus no-codec | RX e TX su porte separate, software reference/ring | Il backend crea due canali `esp_driver_i2s` su porte diverse | Due `io_codec_dev` o IO GMF separati; possibile `esp_asrc` per drift | Clock indipendenti: AEC piu fragile del single-bus codec | Da validare subito nel build/firmware: e' requisito supportato, non caso opzionale |
| TDM bus | IDF TDM config + slot policy YAML | Config TDM passata direttamente a `i2s_channel_init_tdm_mode` | GMF `aud_deintlv`/`aud_intlv` dopo IO | Config bus e slot sono due problemi diversi | Config bus passa al driver IDF, layout frame usa Espressif effects |
| PDM | Non supportato nel full-duplex single-bus | Uguale | `gmf_io_i2s_pdm` esiste | PDM e full-duplex codec non sono lo stesso caso | Fuori dallo step attuale |

### 3. RX: Bus -> Mic/Reference

| Blocco RX | Prima / codice nostro | Ora nel branch | Target GMF | Cosa resta nostro | Knob |
|---|---|---|---|---|---|
| Buffer RX DMA/frame | Prealloc nostri, shape calcolata da processor frame spec | Ancora nostri | GMF payload/ring buffer, ma dimensioni derivate dallo stesso frame spec | Bridge verso callback ESPHome e processor contract | `buffers_in_psram`, futuri GMF buffer size |
| 32-bit -> 16-bit | Loop `sample >> 16` in RX e converter | `esp_ae_bit_cvt` | `aud_bit_cvt` | Solo scelta bus bit-depth | `bits_per_sample`, `slot_bit_width` |
| Stereo split ES8311 | Manual channel pick | `esp_ae_deintlv_process`, poi selezione mic/ref | `aud_deintlv` + channel select/sort | Policy canale ref/mic | `use_stereo_aec_reference`, `reference_channel` |
| TDM slot split ES7210 | Manual strided slot copy | `esp_ae_deintlv_process`, poi selezione slot mic/ref | `aud_deintlv`, forse `esp_gmf_ch_sort` | Policy slot e health monitor | `tdm_mic_slot(s)`, `tdm_ref_slot` |
| Dual mic MMR | Manual interleave mic1/mic2 | `esp_ae_intlv_process` per input AFE | `aud_intlv` o sorter GMF | Solo mapping dei due slot | `tdm_mic_slots` |
| Rate conversion 48k -> 16k | `esp_ae_rate_cvt`, ma con staging manuale | `esp_ae_rate_cvt` con staging Espressif e fail-closed | `aud_rate_cvt` | Niente DSP nostro | `audio_effects.rate_cvt_complexity`, `audio_effects.rate_cvt_perf_type`, `output_sample_rate` |
| Input gain | Scala prima del processor | Q31 esp-audio-libs per attenuazione, saturating scalar solo per boost di calibrazione | ALC usato per `mic_gain` utente; `input_gain` resta board tuning pre-processor | Si, e' board tuning | `input_gain` |
| DC offset | HPF manuale | Uguale | Nessun HPF chiaro nei moduli attuali | Si, finche Espressif non offre drop-in | `correct_dc_offset` |
| Callback mic | Fanout a MWW/VA/intercom | Uguale | GMF output bridge -> callback | Si, e' API ESPHome | Nessun cambio |

### 4. TX: Speaker/Mixer -> Bus

| Blocco TX | Prima / codice nostro | Ora nel branch | Target GMF | Cosa resta nostro | Knob |
|---|---|---|---|---|---|
| Ingresso speaker | ESPHome speaker/mixer scrive mono PCM nel ring | Uguale | GMF input bridge alimentato dallo stesso speaker | Si, API speaker/mixer | `speaker.buffer_duration`, ESPHome mixer |
| Volume software | Q15 scaling nel task | Uguale | Possibile `aud_alc`/`aud_fade` solo come feature | Si per semantica media_player/master volume | output/master volume |
| Mono -> stereo | Loop backward duplicava L/R | `esp_ae_intlv_process` | `aud_intlv` o `aud_ch_cvt` | Solo scelta canale/layout | `num_channels`, `tx_channel` |
| Mono -> TDM slots | Loop zero-fill + slot 0 speaker | `esp_ae_intlv_process` con buffer silenzio preallocato | `aud_intlv` | Policy slot TX | TDM config |
| 16-bit -> 32-bit TX | Loop `sample << 16` | `esp_ae_bit_cvt` | `aud_bit_cvt` | No | `bits_per_sample`, `slot_bit_width` |
| Scrittura bus | `esp_codec_dev_write` diretto | `io_codec_dev` writer via `esp_gmf_io_release_write` | Fatto | Resta nostro solo il conteggio frame consumati dal ring ESPHome | `gmf_io.writer.*` |
| Played-frame callback | Conteggio byte letti dal ring, timestamp | Uguale | Aggiornare dal GMF writer quando sara owner | Si, serve a ESPHome mixer | Nessun cambio |

### 5. Reference AEC

| Modalita reference | Prima / codice nostro | Ora nel branch | Target Espressif/GMF | Rischio da testare | Knob |
|---|---|---|---|---|---|
| ES8311 stereo digital feedback | RX stereo: mic + DAC ref, estrazione manuale | Deinterleave/bit/rate via Espressif, policy invariata | GMF RX pipeline + AEC/AFE input `MR` | Swap canale o ref muto | `use_stereo_aec_reference`, `reference_channel`, `no_dac_ref` |
| ES7210 TDM hardware ref | Slot TDM dedicato per ref, health monitor custom | Deinterleave via Espressif, health monitor resta | GMF RX pipeline; health monitor resta se GMF non lo offre | Slot sbagliato, PGA board errato, ref silenzioso | `use_tdm_reference`, `tdm_ref_slot`, slot-level sensors |
| No-codec previous frame | TX frame precedente come ref software | Rate conversion TX ref via Espressif, fail-closed | GMF bridge deve comunque costruire canale `R` | Delay fisso non sempre allineato | `aec_reference: previous_frame` |
| No-codec ring buffer | Ring ref ritardato a processor rate | Uguale, conversione ref Espressif | GMF bridge/ring equivalente | Drift tra bus separati | `aec_reference: ring_buffer`, `aec_reference_buffer_ms`, `aec_ref_ring_in_psram` |
| Dual mic + ref per AFE | MMR costruito a mano | Mic1/mic2 interleave via Espressif; ref separato | GMF sort/intlv + `esp_gmf_afe_manager` | Ordine `MMR` errato rompe SE/AEC | `tdm_mic_slots`, `tdm_ref_slot` |

### 6. AFE/AEC/AI

| Blocco | Prima / codice nostro | Ora nel branch | Espressif importabile | Decisione |
|---|---|---|---|---|
| Full AFE | Bridge custom feed/fetch su esp-sr | `esp_gmf_afe_manager` gia usato in `esp_afe` | Manager gia ufficiale; GMF `esp_gmf_afe` include WakeNet/state machine | Tenere manager, non importare WakeNet GMF perche MWW/VA sono ESPHome/TFLite |
| Standalone AEC | `afe_aec` low-level wrapper | Uguale | `esp_gmf_aec` GMF element | Valutare dopo GMF IO; non cambiare insieme al bus |
| Wake word | MWW TensorFlow/ESPHome | Uguale | GMF `esp_gmf_wn` | Non importare ora: duplicazione funzionale |
| VAD/NS/AGC/SE | AFE manager/esp-sr | Uguale | Gia in `esp_gmf_afe_manager`/esp-sr | Esporre solo knob realmente supportati e testati |

### 7. Knob Ufficiali Da Esporre

| Modulo upstream | Knob ufficiali | Stato YAML | Regola |
|---|---|---|---|
| `esp_ae_rate_cvt` | `complexity`, `perf_type`, rates, channel, bits | `complexity` e `perf_type` esposti; rates/channel/bits derivati | Esporre solo cio' che l'utente puo' scegliere senza rompere shape audio |
| `esp_ae_bit_cvt` | src bits, dest bits, channel, sample rate | Derivati da bus/config | Non esporre: sarebbe duplicato di `bits_per_sample` |
| `esp_ae_deintlv_process` | channel, bits, sample count | Derivati | Non esporre: l'utente sceglie slot, non parametri interni |
| `esp_ae_intlv_process` | source count, bits, sample count | Derivati | Non esporre: l'utente sceglie canali/TDM |
| `gmf_io` `io_codec_dev` | `io_size`, `buffer_size`, `enable_speed_monitor`, thread stack/prio/core/ext stack, task timeout | Esposti in `gmf_io.reader` e `gmf_io.writer` | Default zero usa IO sincrono ufficiale; valori non-zero abilitano buffer/task GMF |
| `esp_gmf_task` pipeline | task stack/prio/core | Non ancora esposti | Mappare prima sugli attuali `task_*`, poi aggiungere override solo se serve |
| `esp_gmf_afe_manager` | feed/fetch task config, suspend/resume, feature bits | Feature bits gia parzialmente via switches | Audit separato: esporre solo knob stabili e documentati |
| GMF/AE ALC, fade, DRC, MBC, EQ, sonic | Parametri effetto | Non esposti | Feature opzionali future, non requisito del porting backend |

### 8. Azioni Ancora Da Fare

| Priorita | Azione | Perche | File probabili |
|---|---|---|---|
| 1 | Convertire primitive dirette `esp_ae_*` in elementi GMF `aud_*` dove il grafo GMF e' owner | Allinea esempi IDF/GMF e semplifica chaining | nuovo builder pipeline RX/TX |
| 2 | Mappare lifecycle ESPHome su `pipeline_run/stop` e `esp_gmf_afe_manager_suspend/resume` | Stop bus/task quando non ci sono consumer senza perdere API ESPHome | `start()`, `stop()`, consumer registry |
| 3 | Audit knob manager/AFE ufficiali e aggiungere YAML solo quando cablati | Evita opzioni finte | `esp_afe`, README, YAML target |
| 4 | Test board in ordine: ES7210 TDM, ES8311 stereo ref, no-codec dual-bus | Copre i tre casi reference piu diversi | YAML full-experience e experimental |

## Two Migration Tracks

### Track A: Effects Refactor

This keeps the current `audio_stack` audio task and ESPHome lifecycle. It replaces
manual sample manipulation with `esp_audio_effects` modules where the API fits.

Order:

1. Remove registry pins for Espressif audio components. Done.
2. Replace 32-bit-to-16-bit conversion with Espressif bit conversion. Done.
3. Replace TX stereo/TDM layout expansion with Espressif data weaver. Done.
4. Replace TDM/stereo RX deinterleave with Espressif data weaver. Done.
5. Keep ESPHome mixer, speaker ring, consumer registry and AEC reference policy as the API boundary.
6. Expose official knobs when they are actually wired: `audio_effects.rate_cvt_complexity`, `audio_effects.rate_cvt_perf_type` and `gmf_io.reader`/`gmf_io.writer` are live now.

Success criteria:

| Test | Expected result |
|---|---|
| Spotpear ES8311 stereo ref | Same or better echo cancellation, no channel swap. |
| Waveshare S3/P4 TDM ref | Mic slots and ref slot remain correct; health monitor still works. |
| Generic no-codec 32-bit MEMS | No regression in DC offset/gain staging; no extra latency. |
| Mixer + ducking | No user-facing change. |

### Track B: GMF Pipeline Ownership

This moves IO/elements from direct C calls to GMF pipelines inside
`esp_audio_stack` while exposing the same ESPHome microphone and speaker
surface. It is not a separate no-op component and it should not retain a hidden
legacy runtime path.

Target shape:

```text
ESPHome speaker/mixer
  -> speaker bridge ring
  -> optional GMF aud_ch_cvt/aud_bit_cvt/aud_intlv
  -> io_codec_dev TX

io_codec_dev RX
  -> optional GMF aud_deintlv/aud_bit_cvt/aud_rate_cvt
  -> AEC/AFE input staging
  -> esp_gmf_afe_manager or esp_gmf_aec
  -> post-processor bridge ring
  -> ESPHome mic callbacks
```

Important: the bridge rings are not accidental duplication. They are the
boundary between ESPHome's callback/speaker model and GMF's port/pipeline model.
If GMF owns both sides directly, MWW, VA, intercom and ESPHome mixer lose their
normal integration points.

## Open Questions For Prototype

| Question | Why it matters | First answer to test |
|---|---|---|
| Can `io_codec_dev` cleanly share the same ES8311/ES7210 codec handles as current `CodecDevBackend`? | Avoid double-owning codec state. | Answered in code: `CodecDevBackend` opens `esp_codec_dev`, wraps the same handles with `gmf_io/io_codec_dev`, and all RX/TX audio passes through GMF acquire/release. |
| Does GMF add unacceptable latency between RX and AEC reference? | AEC depends on stable mic/ref timing. | Measure with TDM and stereo ref on WS3/P4. |
| Can GMF output bridge deliver fixed 512/1024 sample frames without starving ESPHome callbacks? | MWW/VA expect steady 16 kHz frames. | Use bounded no-split rings and drop/silence on underrun, like `esp_afe` does now. |
| Does `esp_gmf_aec` improve anything over `afe_aec`? | Avoid importing GMF for no benefit. | Test after Track A; not first. |
| Can no-codec dual-bus be represented in a fuller GMF element graph without worse clock skew? | It is already the weak AEC topology. | Bus ownership and codec IO are already Espressif-backed; deeper GMF graph ownership needs hardware latency measurement before replacing the ESPHome bridge rings. |

## Initial Board Target

Use a codec board first:

1. Waveshare S3 Audio or Waveshare P4 Touch with ES7210 + ES8311 TDM.
2. Then Spotpear ES8311 stereo digital feedback.
3. Then generic no-codec MEMS + I2S amp.

The TDM board is the best first target because it exercises dual mic, hardware
reference, 48 kHz bus, 16 kHz processor output and AFE/SE in one build.
