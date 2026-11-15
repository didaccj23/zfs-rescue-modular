# Rescat ZFS: triar i recuperar carpetes o fitxers

**Experimental:** provat amb imatges sintètiques i errors injectats, **no amb un
pool creat per OpenZFS ni amb els teus discs**. Llistar una carpeta no garanteix
que el contingut dels seus fitxers sigui recuperable.

Lector Python offline per a **un únic mirror de dos membres**. Permet navegar
amb el teclat i recuperar **carpetes senceres o fitxers individuals**, sense
extreure abans tot `vol1/DATA` ni necessitar espai per a tot el dataset.

**Guia:** [Preparació](#1-preparació) · [Fonts](#2-fonts-i-dataset) ·
[Selector](#3-triar-amb-el-teclat-i-recuperar) · [Resultats](#4-resultats-i-lots) ·
[Referència tècnica](#referència)

## Inici ràpid: assistent pel terminal

**No cal editar cap codi ni definir variables Bash.** Des d'un terminal Linux,
entra a la carpeta de l'aplicació i executa:

```bash
sudo python3 zfs_rescue.py
```

Amb imatges llegibles i un destí accessible pots ometre `sudo`. L'assistent:

1. Demana la **ruta de cada font**: partició ZFS o imatge. A la segona font pots
   prémer Enter si només tens un membre; en aquest cas no hi ha rèplica alternativa.
2. Demana si cada font és una partició o un **disc complet**. Per a discs complets
   exigeix l'offset i la longitud coneguts de la partició ZFS, **en bytes**; no els
   inventa ni tria automàticament una partició. Si no els saps, cancel·la i consulta
   `probe` segons la referència.
3. Demana el mapa ddrescue de cada font, si en tens; Enter el deixa sense mapa.
4. Demana el dataset —Enter accepta **`vol1/DATA`**— i una **carpeta de destí nova**
   en un disc sa, físicament independent i ja muntat.
5. Mostra el resum i demana `s` per obrir el selector. Llavors valida les fonts i
   el pool i obre el [selector de carpetes i fitxers](#3-triar-amb-el-teclat-i-recuperar).
   **Aquesta primera confirmació no inicia cap còpia**: cal marcar rutes i tornar
   a confirmar dins del selector.

Escriu les rutes **sense cometes**, també si contenen espais. `q` cancel·la a
qualsevol pregunta; Enter a la confirmació final també cancel·la. No es crea
cap destí ni s'obre cap font fins que s'accepta la configuració.

L'assistent requereix entrada i sortida connectades a un terminal interactiu.
No munta discs, no crea imatges i no repara el pool. **Les precaucions i els
límits experimentals de la guia continuen vigents.**

Les ordres amb arguments continuen disponibles. Per demanar només els valors
que falten, també pots fer:

```bash
sudo python3 zfs_rescue.py select --interactive --dataset vol1/DATA
```

## 1. Preparació

- **Conserva els originals.** El més segur és fer una imatge independent de cada
  disc i treballar-hi. Si no tens espai per a dues imatges d'1 TB, pots llegir
  directament les particions en Linux, però **les lectures també poden empitjorar
  un disc avariat**. Si fa sorolls anormals, es desconnecta o es degrada, atura't
  i valora un servei professional.
- **No importis ni muntis el pool original; no facis scrub, actualitzacions ni
  reparacions.** Evita automuntatges i accessos d'altres processos. El programa
  només llegeix les fonts, però no impedeix escriptures alienes o del firmware.
- Utilitza **Linux** —pot ser Live— i **Python 3.10 o posterior**. No calen
  paquets pip. El selector necessita un terminal interactiu d'almenys **80 × 18**
  i el mòdul estàndard `curses`; si el teu Python no l'inclou, instal·la el
  paquet corresponent de la distribució o utilitza les ordres sense interfície.
  A Windows natiu només es pot consultar l'ajuda i executar les proves portables.
- Descomprimeix el paquet i entra a `zfs-rescue-modular`. Mantén `zfs_rescue.py`
  i **tota** la carpeta `zfs_reader` junts, sense barrejar versions o fragments
  monolítics. També pots executar l'entrada amb el camí complet des d'una altra
  carpeta. No cal instal·lar res al TrueNAS.
- El destí ha de ser **emmagatzematge sa, físicament independent dels originals**,
  amb un sistema POSIX que admeti els noms originals, fitxers grans i enllaços
  durs/simbòlics; preferiblement ext4, no FAT/exFAT ni sistemes que alterin noms
  o majúscules. Només cal espai per a la selecció **descomprimida**, més marge
  per a parcials, informes i metadades; no cal que tingui 2 TB.

Comprova Python i que el destí estigui realment muntat:

```bash
python3 --version
python3 zfs_rescue.py --help
findmnt -T /mnt/DESTI_SA
df -h /mnt/DESTI_SA
```

`/mnt/DESTI_SA` és un exemple: **crear una carpeta no munta cap disc**. La carpeta
pare ha d'existir; el destí de cada recuperació, com `DATA-lot1`, **ha de ser nou**,
ni tan sols una carpeta buida existent.

## 2. Fonts i dataset

**Identifica cada disc per model, número de sèrie i mida.** Si ZFS ocupa una
partició, selecciona aquella, no la de swap; no pressuposis el mateix número
de partició als dos discs. Si no en coneixes els límits, no els inventis.

```bash
lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,LOG-SEC,MOUNTPOINTS
ls -l /dev/disk/by-id/
```

Defineix les fonts **una sola vegada** en Bash. Aquest exemple llegeix les
particions originals; substitueix els dos identificadors:

```bash
FONTS=(
  --source /dev/disk/by-id/ID_DISC_1-partN
  --source /dev/disk/by-id/ID_DISC_2-partM
)
```

| Fonts que tens | Configuració |
|---|---|
| Particions ZFS directes | La de l'exemple; offsets zero. |
| Imatges de partició | Canvia els dispositius per les imatges; offsets zero. |
| Discs o imatges **complets** | Cal `probe`, `--offset` i `--length`: obre la primera secció de referència. |
| Imatges amb mapes ddrescue | Afegeix un `--mapfile` correcte per font; no reutilitzis mapes d'altres adquisicions. |

Escaneja i comprova el nom del dataset:

```bash
sudo python3 zfs_rescue.py scan "${FONTS[@]}"
sudo python3 zfs_rescue.py list "${FONTS[@]}"
```

Confirma el pool `vol1` i el dataset exacte `vol1/DATA`; distingeixen majúscules.
Tenir dos discs d'1 TB **no demostra que siguin un mirror**: `scan` valida la
topologia. Un sol membre conegut pot servir, però sense la segona còpia per
rescatar blocs. Cada font proporcionada necessita una configuració llegible.

Les ordres utilitzen `sudo` pels permisos dels dispositius. Amb imatges llegibles
i un destí accessible, executa `python3` sense privilegis; **no canviïs els
permisos dels discs originals**.

## 3. Triar amb el teclat i recuperar

```bash
sudo python3 zfs_rescue.py select "${FONTS[@]}" \
  --dataset vol1/DATA --dest /mnt/DESTI_SA/DATA-lot1 -v
```

El selector mostra el dataset, el **TXG**, el `root-id`, el destí i la carpeta
actual. **No copia res fins que revises la selecció i confirmes.**

| Tecla | Acció |
|---|---|
| **↑ / ↓** o `k` / `j` | Moure el cursor. `PgUp` / `PgDn` salten pàgines; `Inici` / `Fi`, als extrems. |
| **Enter**, → o `l` | **Obrir una carpeta**. Els fitxers no s'obren ni es previsualitzen; els symlinks tampoc no se segueixen. |
| **Espai** | **Marcar/desmarcar una carpeta sencera o un fitxer individual**. |
| **←**, Retrocés o `h` | Tornar a la carpeta pare; les marques es conserven. |
| `i` | Veure la ruta i el destí complets si no caben a la pantalla; no obre el fitxer. Enter o `n` tornen. |
| **`r`** | Revisar totes les rutes marcades. En aquesta pantalla, **`s` confirma la còpia** i `n` torna al selector. |
| **`q`** o Esc | Sortir sense copiar ni crear el destí. |

**Una carpeta marcada inclou TOT el seu contingut i totes les subcarpetes**,
no només el que es veu a la pantalla. No cal entrar-hi per marcar-la. Un fitxer
marcat només selecciona aquell fitxer. La fila `.` marca tota la carpeta actual;
a l'arrel, això selecciona **tot el dataset**.

Les marques signifiquen:

- `[x]`: ruta marcada directament.
- `[*]`: ja inclosa perquè un antecessor està marcat com a carpeta sencera.
- `[-]`: carpeta amb alguns descendents seleccionats, però no sencera.
- `[ ]`: no seleccionada.

Si un fitxer està inclòs per una carpeta sencera, primer has de desmarcar
aquella carpeta per poder triar-ne només alguns descendents. Marcar un pare
unifica les marques dels fills; no hi ha exclusions dins d'una carpeta marcada.
**Amb zero marques no es pot iniciar la recuperació.**

Altres detalls importants:

- Navegar només llegeix metadades de directoris, objectes i atributs: **no llegeix
  continguts de fitxers ni destinacions de symlinks**. Marcar una carpeta no en
  recorre els descendents. Els errors de metadades es mostren; una entrada
  il·legible o especial no es pot marcar individualment.
- El TXG escollit es manté durant tota la sessió i la còpia, sense tornar a
  escanejar. Sense `--txg`, inicialment es pot triar una versió anterior si
  l'arrel més recent és il·legible. Revisa la capçalera; per repetir un estat,
  afegeix `--txg TXG_ESCOLLIT --root-id ROOT_ID_ESCOLLIT` amb valors reals.
- `--path "Fotos"` permet començar en una subcarpeta. Els noms es mostren entre
  cometes i escapats per protegir el terminal; els bytes originals es conserven.
  Els noms o camins llargs truncats amb `...` es poden consultar amb `i`.
- No hi ha catàleg persistent ni previsualització. Es conserva només el directori
  actual, una petita memòria cau i les marques. Tornar a una carpeta pot implicar
  rellegir-ne metadades. En sortir, les marques no es desen.
- Les carpetes **no inclouen datasets fills ni snapshots**: si `list` mostra
  `vol1/DATA/altre` o `vol1/DATA@nom`, recupera'ls per separat amb `--dataset`.
- La revisió no calcula recursivament la mida de les carpetes ni garanteix espai
  suficient. Comprova el destí abans de confirmar; a la referència tens com
  obtenir una estimació lògica amb `browse`.
- El selector requereix entrada i sortida connectades a un terminal, no pipes
  o redireccions. Per automatitzar o desar llistats, utilitza `browse`/`extract`.

## 4. Resultats i lots

```text
DATA-lot1/
├── tree/             # Fitxers complets amb els noms i les rutes originals
├── incomplete/       # Parcials .part identificats per objecte i ID únic
└── manifest.jsonl    # Selecció, TXG, atributs, SHA-256, errors i resum
```

Els complets apareixen un a un a `tree/`. **No moguis, esborris ni reanomenis
entrades mentre s'executa**: poden ser necessàries per crear enllaços durs.
Quan acabi, comprova una mostra de fitxers amb les seves aplicacions normals.

Un `.part` pot retornar **zeros als intervals perduts: no són dades recuperades**.
Els esdeveniments `unreadable_range` en registren els offsets i les longituds;
el parcial no es publica amb el nom original a `tree/`. Els buits legítims
de fitxers sparse sí que es reprodueixen a partir de metadades verificades.

`completed_supported_scope` significa «sense errors dins de la selecció i
l'abast suportat», **no** que tot `vol1/DATA` sigui íntegre. El manifest distingeix
`selected_paths` de `whole_dataset`, enumera les rutes incloses i identifica el
selector amb `selection_method: "terminal"`. Queden fora ACL, xattrs, altres
datasets, dades només al ZIL i fitxers absents de les metadades llegibles.

| Sortida | Significat |
|---|---|
| `0` | Ordre completada dins del seu abast, o selector cancel·lat abans de copiar. `probe` pot informar que no hi ha GPT vàlida. |
| `1` | Error fatal: configuració incompatible, cap TXG útil, terminal o destí invàlid, límit de recursos… |
| `2` | Llistat/extracció parcial; també arguments incorrectes d'argparse. |
| `130` | Ctrl+C: execució interrompuda, resultats incomplets. |

**Sense resum final, la còpia no s'ha completat.** Una interrupció pot deixar
`.part` sense entrada final al manifest. Cancel·lar el selector abans de copiar
només genera l'esdeveniment `cancelled`, sense crear el destí.

Per a una altra selecció o TXG, utilitza **un destí nou**, com `DATA-lot2`.
No hi ha represa, fusió ni reparació automàtica de parcials d'altres execucions.
Compara manifestos i hashes abans d'unificar resultats. Pots alliberar espai
després d'acabar i comprovar un lot, conservant-ne abans les dades en un altre
mitjà sa.

## Referència

Els detalls següents són desplegables per no interrompre la guia principal.

<details>
<summary><strong>Imatges ddrescue, mapes i offsets de discs complets</strong></summary>

Fer imatges independents continua sent l'opció més segura. Primera passada en
Linux, amb identificadors reals i emmagatzematge sa:

```bash
sudo ddrescue -n /dev/disk/by-id/ID_DISC_1 /mnt/RESCAT/disc1.img /mnt/RESCAT/disc1.map
sudo ddrescue -n /dev/disk/by-id/ID_DISC_2 /mnt/RESCAT/disc2.img /mnt/RESCAT/disc2.map
```

Les sortides han de ser diferents i noves, o correspondre exactament als mapes
de represa; **mai copiïs un membre sobre l'altre**. `-n` evita scraping i passades
de reintent: no afegeixis reintents intensius automàticament a discs que empitjoren.
Una fallada física greu requereix una estratègia específica. El lector Python
no executa ddrescue.

També pots adquirir només les particions ZFS si en coneixes amb certesa els
límits. Per a **imatges de partició**, defineix `FONTS` així:

```bash
FONTS=(
  --source /mnt/RESCAT/zfs1.img
  --source /mnt/RESCAT/zfs2.img
)
```

Els offsets són zero. Afegeix, si en tens, un `--mapfile` d'aquella adquisició
per font. Amb permisos suficients, les ordres de la guia no necessiten `sudo`.

Per a **imatges de disc complet**, consulta primer la GPT de cada disc:

```bash
python3 zfs_rescue.py probe --source /mnt/RESCAT/disc1.img
python3 zfs_rescue.py probe --source /mnt/RESCAT/disc2.img
```

- Amb sectors lògics de 4096 bytes, afegeix `--sector-size 4096`.
- `probe` verifica CRC de capçalera i taula de les dues còpies GPT; no en modifica
  cap ni tria automàticament la partició ZFS. Si discrepen, resol els límits abans
  de continuar. Si queden fora dels sectors adquirits, calen límits coneguts
  d'una altra font. No hi ha autodetecció MBR.
- El `+4 MiB` dels DVA és relatiu a l'inici del **vdev ZFS**, no necessàriament
  al disc físic. ZFS a TrueNAS acostuma a ocupar una partició.

Substitueix els marcadors per nombres **en bytes**, un offset i una longitud per
font en el mateix ordre; poden ser diferents entre discs:

```bash
FONTS=(
  --source /mnt/RESCAT/disc1.img --source /mnt/RESCAT/disc2.img
  --offset OFFSET_1_BYTES --offset OFFSET_2_BYTES
  --length LONGITUD_1_BYTES --length LONGITUD_2_BYTES
  --mapfile /mnt/RESCAT/disc1.map --mapfile /mnt/RESCAT/disc2.map
)
```

`--length` és la longitud de la partició/vdev, no de l'input complet. Aquesta
configuració també serveix per a discs complets raw, amb les precaucions de la guia.
Repeteix `FONTS` a totes les ordres.

Els mapes GNU ddrescue són opcionals però recomanables: només es llegeixen
intervals `+`. Els seus offsets són absoluts de l'input adquirit: **un mapa de
disc complet no serveix directament per a una imatge retallada de partició**.
Si passes mapes, cal un per font; `--mapfile -` significa sense mapa.
Mantén les fonts immutables: **no executis ddrescue sobre les mateixes imatges
mentre el lector les utilitza**.

</details>

<details>
<summary><strong>TXG, llistats, estimació de mida i extracció sense interfície</strong></summary>

`scan` mostra uberblocks verificats, TXG i hora UTC. `list` enumera datasets i
snapshots. Per comprovar el MOS i l'arrel d'un dataset:

```bash
sudo python3 zfs_rescue.py check "${FONTS[@]}" --dataset vol1/DATA
```

**`check` no llegeix tots els fitxers ni tots els descendents.** Sense filtres,
el lector prova TXG de més recent a més antic fins a trobar el MOS i l'arrel
llegibles. Un TXG anterior representa dades antigues, amb possibles canvis
posteriors absents; comprova l'esdeveniment `selected`. `--txg` és estricte i
mai no tria un altre estat; `--max-txg` exclou transaccions posteriors a un límit.
Si hi ha arrels diferents al mateix TXG, calen `--txg` i `--root-id` de `scan`;
no es pressuposa que el número més alt sigui el millor.

Llista l'arrel sense copiar fitxers i anota el TXG i el `root-id` reals:

```bash
sudo python3 zfs_rescue.py browse "${FONTS[@]}" \
  --dataset vol1/DATA --directories-only --human
```

Per entrar en una carpeta del mateix estat, substitueix els marcadors:

```bash
sudo python3 zfs_rescue.py browse "${FONTS[@]}" \
  --dataset vol1/DATA --txg TXG_ESCOLLIT --root-id ROOT_ID_ESCOLLIT \
  --path "Fotos" --human
```

`browse` llegeix només metadades i, per defecte, un nivell. Sense
`--directories-only` també mostra fitxers i mides; `--recursive` visita els
descendents i fa més lectures. No desa un índex ni comprova els continguts.
Tret de `--human`, la sortida és JSONL i els diagnòstics van a stderr.
Desa qualsevol redirecció **només en emmagatzematge sa**: la fa el shell,
fora de la protecció de destí del programa.

Per estimar la mida d'una carpeta, afegeix `--recursive` a l'ordre anterior.
`counts.logical_file_bytes` suma mides lògiques dels fitxers visitats, **comptant
cada ruta**, inclosos hardlinks repetits. Sense recursió només compta el nivell
llistat. No prediu bytes físics ni recuperables: sparse, enllaços, compressió,
deduplicació, parcials i metadades alteren l'espai necessari. Reserva marge.

Còpia selectiva sense selector:

```bash
sudo python3 zfs_rescue.py extract "${FONTS[@]}" \
  --dataset vol1/DATA --txg TXG_ESCOLLIT --root-id ROOT_ID_ESCOLLIT \
  --include "Fotos/2020" --include "Documents/nota.txt" \
  --dest /mnt/DESTI_SA/DATA-lot1 -v
```

- `--include` és repetible: ruta exacta i descendents, tant carpetes com fitxers.
  Exigeix `--txg`; repeteix també el `root-id` per reproduir l'estat sense ambigüitat.
- Les rutes són relatives al dataset: `"Fotos/2020"`, **no** `"/Fotos/2020"` ni
  `"vol1/DATA/Fotos/2020"`. Utilitza `/`. No hi ha comodins: `"Fotos/*"` és literal.
- `Fotos` no selecciona `Fotos_backup`. Les seleccions solapades s'unifiquen;
  es creen antecessors, però no es copien germans exclosos. Sí que es llegeixen
  metadades d'antecessors, que poden compartir blocs amb entrades excloses.
- Les rutes inexistents, il·legibles o que travessen symlinks es rebutgen **abans
  de crear el destí**, també després de confirmar al selector. Un symlink
  seleccionat es recupera com a enllaç, mai seguint-ne el contingut.
- **Sense cap `--include`, `extract` intenta recuperar TOT el dataset.**
  `--include "."` també selecciona tot. Per fer-ho, omet els `--include` de
  l'exemple i reserva prou espai. Els datasets fills continuen sent independents.

`--sector-recovery` permet provar una altra execució sobre les imatges i els dos
mapes quan hi ha errors d'E/S complementaris: combina sectors llegibles de
512 bytes i exigeix el checksum final correcte. Fa més lectures i no prova
totes les combinacions de corrupció silenciosa. Si les dues còpies retornen
bytes erronis sense error d'E/S en sectors diferents, pot no trobar el bloc;
**sense checksum coincident el rebutja**, no el presenta com a recuperat.

</details>

<details>
<summary><strong>Formats admesos i límits del lector</strong></summary>

**Implementat:**

- Fonts `os.O_RDONLY` i lectures `os.pread`; no importa ni munta pools ni executa
  eines ZFS. Llegeix les quatre etiquetes de cada membre, verifica SHA-256 de
  configuracions/uberblocks, l'offset verificador relatiu al vdev i tota la ranura.
- Valida GUID de pool, vdev superior i membres, obté `ashift` i rebutja inputs
  duplicats o del mateix membre. Necessita etiquetes coherents; no les reconstrueix.
- DVA i tres emplaçaments del BP, byte order, embedded blocks i indirectes.
  Verifica cada bloc físic **abans de descomprimir**; errors d'E/S, lectures curtes
  o checksum incorrecte fan provar altres còpies i DVA.
- Fletcher2, Fletcher4, SHA-256 i SHA-512/256 quan OpenSSL el proporciona.
  Sense compressió, LZ4 ZFS —prefix big-endian de quatre bytes—, LZJB, gzip/zlib
  i ZLE; no necessita el paquet `lz4`.
- MOS → directoris/datasets/snapshots DSL → object set ZPL → dnodes → ZAP.
  Micro/fat-ZAP i taules de punters externes; fulles de directori perdudes es
  poden informar mentre es recuperen les accessibles.
- Znodes antics, SA amb registres/layouts dinàmics —sense IDs fixos—,
  spill blocks i dnodes grans. Fitxers regulars, buits legítims, carpetes,
  hardlinks entre fitxers recuperats i symlinks; noms preservats en bytes en POSIX.
- Informe JSONL d'objectes, rutes, errors, SHA-256 dels complets i intervals perduts.
  UID/GID i modes originals es registren però **no s'apliquen**; poden requerir
  resolució FUID. Fitxers nous amb `0600`, carpetes amb `0700`; s'aplica l'mtime
  regular si és representable, sense prometre equivalència completa POSIX/ZFS.

**No implementat:**

- Xifrat ZFS o GELI de TrueNAS CORE, claus o desxifrat; ZSTD; Skein, Edon-R,
  BLAKE3 o blocs amb checksum desactivat. No es presenten com a verificats.
- Stripe, RAIDZ/dRAID, gang blocks, vdevs indirectes per eliminació, topologies
  `replacing`/`spare` o diversos vdevs superiors, inclosos possibles logs/especials.
- Reconstrucció heurística de MOS/ZAP destruïts, fitxers esborrats, carving per
  signatures o escaneig de tot l'espai físic.
- Replay del ZIL; restauració d'ACL, xattrs, fluxos alternatius SMB, propietaris
  o permisos originals; dispositius especials, sockets o FIFOs.
- Extracció conjunta de datasets fills/recreació dels mountpoints, barreja de TXG
  o represa/reparació automàtica de parcials d'altres execucions.

Un uberblock vàlid **no garanteix que els seus descendents encara existeixin**:
blocs antics poden haver estat reutilitzats. Es verifiquen els que es visiten
i es registren els errors. Cal l'arrel del dataset llegible per triar un TXG;
fragments d'una arrel destruïda poden quedar fora de l'abast del lector.

</details>

<details>
<summary><strong>Seguretat real i recursos en un equip de 8 GB</strong></summary>

`O_RDONLY` només impedeix escriure mitjançant **els descriptors d'aquest programa**:
no bloqueja altres processos, un administrador, automuntatges o firmware.
Llegir estressa els discs; treballa amb imatges i conserva els originals offline
o protegits contra escriptura, sempre que sigui possible.

En Linux es comprova l'ascendència dels dispositius raw i es rebutja un destí
dependent d'una font, o una dependència que no es pot determinar. A FreeBSD
l'extracció exigeix **imatges regulars** perquè no hi ha aquest control; la
consulta de mida de dispositius està implementada però no provada en FreeBSD real.
Amb imatges, separar els mitjans físics és responsabilitat teva. Les fonts
mai s'obren per escriure i han de romandre immutables.

Les escriptures estan ancorades a descriptors de directori amb `O_NOFOLLOW` i
creació exclusiva. Es rebutgen components com `..` i `/`; no se segueixen
symlinks recuperats. Un enllaç absolut o que surt de l'arbre es conserva:
**altres eines sí que el podrien seguir** quan examinis els resultats.

No es carrega el dataset ni un fitxer sencer en RAM. Es processen directoris
individualment i s'emeten resultats en flux:

| Recurs | Límit / ajust |
|---|---|
| Bloc / memòria cau de blocs | 32 MiB / 64 MiB. |
| Fitxer | 16 TiB; `--max-file-size`. |
| Entrades visitades | 2.000.000; `--max-entries`. Al selector compta les entrades dels directoris oberts, incloses revisites; la còpia té un recompte independent. |
| Directori | 100.000 entrades/fulles; `--max-dir-entries`, fins a 2.000.000. També s'aplica a l'arrel. |
| Profunditat | 256 components. |
| Rutes pendents / hardlinks | Pressupostos estimats de 64 MiB cadascun; només es recorden hardlinks de fitxers amb més d'un enllaç. |
| Selector | Un directori, 256 entrades d'atributs en memòria cau i un pressupost estimat de 64 MiB per a marques. |

Augmentar `--max-dir-entries` augmenta memòria i lectures. Si l'arrel supera el
límit, `--path` a una subcarpeta no evita haver de llegir aquella arrel.
Els pressupostos de rutes aturen explícitament l'execució; no descarten dades
per aparentar èxit. **No són un límit estricte de RAM total**: Python, estructures
ZFS i memòries cau també en consumeixen. No s'ha mesurat una recuperació real
d'1 TB amb 8 GB ni rendiment de diversos TiB; Python pot ser lent. Comença per
un lot petit i evita llistats recursius globals innecessaris.

</details>

<details>
<summary><strong>Estructura del paquet, proves i diagnòstic</strong></summary>

```text
zfs-rescue-modular/
├── zfs_rescue.py          # Entrada CLI
├── zfs_reader/
│   ├── __init__.py        # Paquet
│   ├── base.py            # Errors, checksums, descompressió, XDR
│   ├── sources.py         # Fonts de només lectura i mapes ddrescue
│   ├── blocks.py          # Punters, rèpliques i uberblocks
│   ├── scan.py            # Etiquetes i candidats
│   ├── objects.py         # Dnodes, object sets, ZAP
│   ├── pool.py            # Datasets, snapshots, atributs
│   ├── selection.py       # Rutes i pressupost de recorregut
│   ├── browse.py          # Llistat de metadades
│   ├── tui.py             # Selector interactiu, sense llegir continguts
│   ├── wizard.py          # Assistent de fonts, dataset i destí pel terminal
│   ├── extract.py         # Destí i extracció
│   └── commands.py        # Arguments i execució
├── tests/
│   ├── __init__.py
│   ├── test_selection.py
│   ├── test_tui.py
│   ├── test_wizard.py
│   └── test_zfs_rescue.py
├── README.md
├── AUDITORIA_CA.md
├── SOURCES.md
└── TEST_RESULTS.txt
```

**Abans d'utilitzar fonts reals, executa en Linux:**

```bash
python3 -B -m unittest discover -v
```

Les proves utilitzen només fitxers temporals sintètics, mai discs físics.
Cobreixen bytes, checksums i compressió —LZ4 contrastat amb `liblz4` si existeix—,
endianness, SA dinàmic/spill, dnodes grans, indirectes, offsets, GPT/CRC, DVA/rèpliques,
E/S complementària, mapes, sparse, enllaços, noms no UTF-8, parcials, protecció de
rutes, ENOSPC i invariància dels hashes de les fonts. El selector afegeix proves
de teclat, marques, revisió/confirmació, cancel·lació, límits i curses amb PTY.

**Els resultats i les omissions exactes són a `TEST_RESULTS.txt`.** En Windows
s'executen proves portables amb E/S i pantalla adaptades: **no validen el kernel
POSIX ni el terminal real**. Les proves d'integració i curses/PTY s'hi ometen i
cal reexecutar-les en Linux. Les imatges estan codificades segons el format
documentat, **no generades per OpenZFS**; tampoc s'han provat discs de l'usuari,
FreeBSD real o una màquina de recuperació de 8 GB.

`SHA256SUMS.txt` no es distribueix al repositori; si en conserves una còpia antiga,
**està desactualitzada** i no valida aquesta revisió.
Consulta també [l'auditoria](AUDITORIA_CA.md) i les [fonts del format](SOURCES.md).

Si falla el cas real, conserva originals, mapes, sortida de `scan`, TXG/root-id i
diagnòstic exacte. No desactivis checksums per obtenir una aparent recuperació
satisfactòria. Cal contrastar una mostra i el manifest amb dades conegudes o
una eina que llegeixi el mateix pool; no hi ha garantia de recuperació.

</details>
