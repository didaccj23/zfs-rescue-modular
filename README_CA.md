# Recuperació ZFS en Python: lector offline per a un mirror de dos discs

**Estat: experimental. Provat amb imatges sintètiques i errors injectats, no amb
els teus discs ni amb una imatge produïda per un pool OpenZFS real.**

El programa adjunt original no recuperava un dataset: només desava el bloc
`objset_phys` del MOS. Aquesta versió implementa el recorregut fins als fitxers,
però no és un recuperador universal de ZFS. Si el teu pool utilitza un format no
suportat o ha perdut metadades imprescindibles, ho indica en lloc d'inventar dades.

## Instal·lació i estructura modular

Descomprimeix `zfs-rescue-modular.zip` i entra a la carpeta `zfs-rescue-modular`.
Conserva aquesta estructura completa; el fitxer principal necessita la carpeta
`zfs_reader` al seu costat. No barregis fitxers d'aquesta versió amb fragments
anteriors ni amb el programa monolític.

```text
zfs-rescue-modular/
├── zfs_rescue.py            # entrada de les ordres
├── zfs_reader/
│   ├── __init__.py          # paquet Python
│   ├── base.py              # errors, checksums, descompressió i XDR
│   ├── sources.py           # fonts de només lectura i mapes ddrescue
│   ├── blocks.py            # punters, rèpliques i uberblocks
│   ├── scan.py              # etiquetes i selecció de candidats
│   ├── objects.py           # dnodes, object sets i ZAP
│   ├── pool.py              # datasets, snapshots i atributs
│   ├── extract.py           # destinació i extracció
│   └── commands.py          # arguments i execució de les ordres
├── test_zfs_rescue.py
├── README_CA.md
├── AUDITORIA_CA.md
├── SOURCES.md
├── TEST_RESULTS.txt
└── SHA256SUMS.txt
```

Des de la carpeta descomprimida:

```bash
sha256sum -c SHA256SUMS.txt
python3 zfs_rescue.py --help
python3 -m unittest -v test_zfs_rescue
```

A FreeBSD, es pot verificar un fitxer amb `sha256 nom_del_fitxer` i comparar-lo
amb el valor corresponent del manifest. Les ordres de recuperació que es mostren
més avall continuen utilitzant `python3 zfs_rescue.py`.
També pots executar el programa des d'una altra carpeta indicant el camí complet
al fitxer principal. Les importacions internes són relatives al paquet.

La divisió conserva els cossos de les 36 funcions i classes del lector anterior.
Les proves importen ara els mòduls corresponents; la injecció de fallades també
s'aplica al mòdul que realment fa l'operació. S'afegeix una prova que executa
l'entrada en un procés separat, des d'una altra carpeta, extreu dades sintètiques
amb una rèplica corrupta i comprova la invariància de les dues imatges.

## Recomanació per al teu cas

Amb sectors danyats en tots dos discs, la prioritat és conservar-ne el màxim de
contingut. Fes una imatge independent de cada disc en emmagatzematge sa i treballa
amb les imatges. No importis el pool, no hi facis un scrub, no l'actualitzis i no
executis operacions de reparació sobre els originals per provar aquest lector.

Que UFS Explorer pugui llegir el dataset és un bon senyal, però no certifica tots
els blocs de tots els fitxers. Tampoc no demostra que l'actualització de TrueNAS
sigui la causa de la corrupció: cal distingir la seqüència dels fets de la causa,
especialment amb errors físics ja coneguts.

Si l'objectiu prioritari és recuperar dades insubstituïbles, una eina que ja ha
demostrat que pot llegir aquest pool, o un servei professional si els discs es
degraden, ofereix més confiança que un lector nou. Aquest paquet satisfà la
petició d'una implementació Python independent, amb límits explícits. No el
consideris més fiable que UFS Explorer només perquè tingui més comprovacions que
el codi anterior.

## Què implementa

- Fonts obertes exclusivament amb `os.O_RDONLY`; lectures amb `os.pread`.
  No importa el pool, no el munta i no executa eines ZFS.
- Lectura independent de les quatre etiquetes de **cada** membre. Verifica els
  checksums SHA-256 de les configuracions i dels uberblocks, inclòs el verificador
  basat en l'offset relatiu al vdev i la mida completa de la ranura.
- Validació de GUID del pool, vdev superior i membres, i obtenció d'`ashift`.
  Rebutja dos inputs que siguin el mateix membre.
- Cerca de TXG de més recent a més antic fins que el MOS i l'arrel del dataset
  sol·licitat siguin llegibles. `--txg` és estricte: si no serveix, no en tria un
  altre. `--max-txg` permet excloure les transaccions posteriors a un límit conegut.
- Descodificació de DVA, els tres emplaçaments possibles del BP, byte order,
  blocs integrats (*embedded*) i punters indirectes.
- Verificació de cada bloc físic abans de descomprimir-lo. Un checksum incorrecte,
  una lectura curta o un error d'E/S provoquen la prova d'una altra còpia.
- LZ4 de ZFS amb el prefix de longitud big-endian de quatre bytes; també sense
  compressió, LZJB, gzip/zlib i ZLE. No requereix instal·lar `lz4`.
- Checksums Fletcher2, Fletcher4, SHA-256 i SHA-512/256 quan OpenSSL el proporciona.
- MOS → DSL directories/datasets/snapshots → object set ZPL → dnodes → ZAP.
  Llegeix micro-ZAP, fat-ZAP i taules de punters externes; les fulles il·legibles
  d'un directori es poden informar mentre s'extreuen les altres fulles accessibles.
- Metadades de znode antic i SA, registres/layouts SA dinàmics, spill blocks i
  dnodes grans. No pressuposa IDs fixos per als atributs SA.
- Fitxers regulars, buits legítims, carpetes, enllaços durs entre fitxers recuperats
  i enllaços simbòlics. Manté els bytes dels noms en un sistema POSIX compatible.
- Informe JSONL amb errors, objectes, rutes, SHA-256 dels fitxers complets i
  intervals il·legibles. Un fitxer parcial mai no apareix com a complet a `tree/`.
- Mapes GNU ddrescue opcionals: només accepta com a llegits els intervals `+`.
- Opció `--sector-recovery` per combinar sectors llegibles de còpies que tenen
  errors d'E/S complementaris; sempre exigeix el checksum final correcte.

## Límits que cal comprovar abans

Aquest lector està pensat per a **un únic vdev superior mirror de dos membres**,
amb almenys una configuració d'etiqueta vàlida en cada input proporcionat. Pot
treballar amb un sol membre conegut, però aleshores no disposa del segon per
rescatar blocs. Exigeix coherència de les etiquetes; no les reconstrueix.

No implementa:

- Xifrat natiu ZFS ni GELI de TrueNAS CORE; no gestiona claus ni desxifra dades.
- ZSTD; checksums Skein, Edon-R o BLAKE3; blocs amb checksum desactivat. Un bloc
  sense checksum no es presenta com a verificat.
- Gang blocks, RAIDZ/dRAID, vdevs indirectes per eliminació de dispositius,
  topologies `replacing`/`spare` ni pools amb diversos vdevs superiors, inclosos
  possibles vdevs de log o especials.
- Reconstrucció heurística de MOS/ZAP destruïts, cerca de fitxers esborrats,
  recuperació per signatures ni exploració de tot l'espai físic.
- Reproducció del ZIL, restauració d'ACL, xattrs, fluxos alternatius de SMB,
  propietaris, permisos originals o dispositius especials. No crea sockets/FIFOs.
- Recuperació recursiva de **datasets fills** ni recreació dels seus mountpoints.
  Cada dataset és un object set independent: extreu-los per separat.
- Barreja de versions de fitxers procedents de TXG diferents en una mateixa
  execució. Tampoc no fa una recuperació automàtica de parcials d'una altra
  execució, ni una represa de la destinació existent.

Els UID/GID i modes es registren com a valors originals, però no s'apliquen; els
identificadors poden necessitar resolució FUID. Els fitxers es creen amb 0600 i
les carpetes amb 0700. S'aplica l'mtime dels fitxers regulars si és representable;
no es promet equivalència completa de metadades POSIX/ZFS.

Un uberblock amb checksum vàlid no garanteix que totes les seves dades encara
existeixin. Els blocs antics poden haver estat reutilitzats. El lector també
valida els checksums dels descendents que visita i registra els que fallen.
L'arrel del dataset ha de ser llegible per seleccionar un TXG; si només queden
fragments d'aquesta arrel, aquesta versió pot no arribar a extreure'ls.

La combinació per sectors no prova totes les combinacions possibles de corrupció
silenciosa. Si tots dos discs retornen bytes incorrectes sense error d'E/S en
sectors diferents, pot no trobar la combinació correcta. Sense un checksum
coincident, rebutja el bloc.

## Seguretat: abast real

`O_RDONLY` impedeix escriure **mitjançant els descriptors de les fonts d'aquest
programa**. No bloqueja altres processos, automuntatges, el firmware o canvis que
un administrador faci als dispositius. La lectura també pot estressar un disc
avariat: per això es recomanen imatges, preferiblement originals conservats
offline o protegits contra escriptura. No s'executa cap rescat dels teus discs en
el procés de preparació d'aquest paquet.

La destinació ha de ser una carpeta nova en un sistema de fitxers sa i separat
dels discs origen. El programa no sobreescriu una carpeta de rescat existent.
En Linux comprova l'ascendència de dispositius per a fonts raw i rebutja una
destinació dependent d'un disc origen, o una dependència que no pot determinar.
En FreeBSD l'extracció exigeix imatges regulars perquè aquest control de
dispositius no s'ha implementat. La lectura/prova de dispositius FreeBSD sí que
disposa de consulta de mida, però no s'ha provat en un sistema FreeBSD real.

Per a inputs que ja són fitxers d'imatge, la separació de mitjans físics és
responsabilitat de l'operador. Les imatges no s'obren mai per escriure. Mantén les
fonts immutables mentre duri l'extracció; no executis ddrescue simultàniament sobre
les mateixes imatges que llegeix el rescatador.

Els components de les rutes s'obren amb descriptors de directori i `O_NOFOLLOW`.
Es rebutgen noms com `..` i `/`; no se segueixen symlinks recuperats. Un symlink
absolut o que apunta fora de l'arbre es conserva com a tal: tingues-ho present
quan després naveguis pels fitxers amb altres eines.

## Requisits

- Python 3.10 o posterior; biblioteca estàndard, sense paquets pip obligatoris.
- Preferiblement Linux, executant el programa com a usuari sense privilegis
  sobre imatges que pugui llegir. No modifiquis l'appliance TrueNAS per instal·lar
  aquest paquet: pots treballar en una altra màquina.
- Sistema de fitxers de destinació POSIX que admeti els noms originals, enllaços
  durs/simbòlics i fitxers grans. No es recomana FAT/exFAT ni una destinació que
  canviï majúscules/minúscules o normalitzi noms de manera incompatible.
- Espai per a les imatges i per a les dades **descomprimides**. Reserva també espai
  per a parcials i informes. Un error d'escriptura atura l'execució.

## 1. Preparar imatges, si encara no les tens

Exemples orientatius de GNU ddrescue en Linux. **Substitueix els identificadors
permanents i confirma que `/mnt/RESCAT` és emmagatzematge sa.** Els dos fitxers de
sortida han de ser diferents, nous o corresponents exactament als seus mapes de
represa. No copiïs mai un membre sobre l'altre.

```bash
sudo ddrescue -n /dev/disk/by-id/ID_DISC_1 /mnt/RESCAT/disc1.img /mnt/RESCAT/disc1.map
sudo ddrescue -n /dev/disk/by-id/ID_DISC_2 /mnt/RESCAT/disc2.img /mnt/RESCAT/disc2.map
```

Aquesta és una primera passada sense scraping ni passades de reintent. No
afegeixis reintents intensius automàticament a discs que es desconnecten, fan
sorolls anormals o empitjoren. Els casos de fallada física greu requereixen una
estratègia específica. Les ordres d'aquest document són exemples per a tu: el
script Python no executa ddrescue.

També pots adquirir imatges de les particions ZFS si ja n'has identificat amb
certesa els límits. En aquest cas, els offsets del lector són zero i els mapes
ddrescue han de correspondre a aquestes adquisicions de partició.

## 2. Distingir una imatge de disc d'una imatge de partició

En TrueNAS és habitual que ZFS ocupi una partició, no que comenci al byte zero
del disc complet. El `+4 MiB` dels DVA és relatiu a l'inici del vdev ZFS, no
necessàriament al disc físic.

Per a una imatge de **disc complet**, consulta la GPT:

```bash
python3 zfs_rescue.py probe --source /mnt/RESCAT/disc1.img
python3 zfs_rescue.py probe --source /mnt/RESCAT/disc2.img
```

Si el disc tenia sectors lògics de 4096 bytes, afegeix `--sector-size 4096`.
El programa valida CRC de capçalera i taula, tant de la GPT principal com de la
còpia final. No determina automàticament quina partició cal utilitzar ni modifica
la GPT. Si les còpies discrepen, identifica els límits abans de continuar. Una
GPT vàlida també pot quedar fora dels sectors recuperats: pots necessitar els
límits coneguts d'una altra font. No hi ha autodetecció MBR en aquest lector.

Els camps `offset` i `length` són bytes. Proporciona **un de cada per disc**, en
el mateix ordre que `--source`. L'offset pot ser diferent entre membres.

```bash
python3 zfs_rescue.py scan \
  --source /mnt/RESCAT/disc1.img --source /mnt/RESCAT/disc2.img \
  --offset OFFSET_1_BYTES --offset OFFSET_2_BYTES \
  --length LONGITUD_1_BYTES --length LONGITUD_2_BYTES \
  --mapfile /mnt/RESCAT/disc1.map --mapfile /mnt/RESCAT/disc2.map
```

Substitueix els quatre marcadors per nombres reals. `--length` és la mida de la
partició/vdev, no la mida de l'input complet. Els mapes utilitzen offsets
absoluts de l'input de ddrescue; un mapa d'un disc complet no serveix directament
per a una imatge retallada de la seva partició.

## 3. Escanejar, llistar i comprovar

Els exemples següents assumeixen **imatges de partició ZFS**, sense necessitat
d'offsets. Si són discs complets, repeteix en cada ordre els offsets, longituds
i mapes correctes del pas anterior. Els mapes són opcionals, però recomanables.

```bash
python3 zfs_rescue.py scan \
  --source /mnt/RESCAT/zfs1.img --source /mnt/RESCAT/zfs2.img

python3 zfs_rescue.py list \
  --source /mnt/RESCAT/zfs1.img --source /mnt/RESCAT/zfs2.img

python3 zfs_rescue.py check \
  --source /mnt/RESCAT/zfs1.img --source /mnt/RESCAT/zfs2.img \
  --dataset tank/dades
```

Substitueix `tank/dades` pel nom exacte del teu dataset. `list` també mostra
snapshots; pots seleccionar `tank/dades@nom_snapshot`.

La sortida és JSONL; els diagnòstics van a stderr. `scan` mostra els uberblocks
verificats, el TXG i l'hora UTC. **`check` només comprova el MOS i l'arrel del
dataset; no llegeix tots els fitxers ni acredita una recuperació completa.**

Sense filtres, es proven els TXG en ordre descendent. Si el més recent falla, es
pot seleccionar un d'anterior: això representa una versió anterior del dataset,
amb possible absència dels canvis posteriors. Revisa l'esdeveniment `selected`.

Per reproduir exactament una prova, fixa el TXG:

```bash
python3 zfs_rescue.py check \
  --source /mnt/RESCAT/zfs1.img --source /mnt/RESCAT/zfs2.img \
  --dataset tank/dades --txg TXG_ESCOLLIT
```

Si apareixen arrels diferents per al mateix TXG, el programa exigeix
`--txg` i `--root-id` de `scan` per evitar una selecció ambigua. No assumeix
que la transacció més alta sigui la millor.

## 4. Extreure

La carpeta pare de destinació ha d'existir; la carpeta `rescat-txg` encara no.

```bash
python3 zfs_rescue.py extract \
  --source /mnt/RESCAT/zfs1.img --source /mnt/RESCAT/zfs2.img \
  --dataset tank/dades --txg TXG_ESCOLLIT \
  --dest /mnt/DESTI_SA/rescat-txg -v
```

Si les imatges tenen sectors no recuperats en llocs complementaris, es pot provar
una nova execució amb `--sector-recovery` i els dos mapes. Aquesta opció fa més
lectures; és preferible utilitzar-la sobre imatges. No converteix sectors perduts
en dades vàlides i pot no rescatar un bloc amb corrupció silenciosa combinada.

Hi ha límits defensius: 16 TiB per fitxer, dos milions d'entrades visitades i
profunditat 256. Els dos primers es poden ajustar amb `--max-file-size` i
`--max-entries`. El límit de mida de bloc és 32 MiB. El lector Python pot ser lent
amb volums grans; no s'ha mesurat rendiment en recuperacions de diversos TiB.

La destinació conté:

```text
rescat-txg/
  tree/                 # Contingut del dataset amb noms i carpetes originals
  incomplete/           # Parcials identificats per objecte i ID únic
  manifest.jsonl        # Resultats, atributs, hashes, errors i resum
```

Un `.part` pot contenir buits en els intervals il·legibles; en llegir-lo, el
sistema operatiu hi retornarà zeros. **Aquests zeros NO són dades recuperades.**
Els esdeveniments `unreadable_range` indiquen exactament els intervals; el fitxer
no es publica al seu nom original a `tree/`. Els buits legítims de fitxers sparse
sí que es reprodueixen a partir de punters de metadades verificades.

Un resum `completed_supported_scope` significa que no hi ha errors en les
entrades processades dins de l'abast suportat; no inclou ACL, xattrs, altres
datasets, contingut només present al ZIL ni fitxers desapareguts de metadades
anteriors. Si no hi ha un resum final, l'execució no s'ha completat. Una
interrupció pot deixar `.part` encara no referenciats per una entrada final.

Codis de sortida:

| Codi | Significat |
|---|---|
| 0 | Ordre completada dins del seu abast; `probe` pot informar que no hi ha GPT vàlida. |
| 1 | Error fatal, configuració incompatible, cap TXG útil o error de destinació. |
| 2 | Extracció parcial amb errors registrats; argparse també utilitza 2 per arguments incorrectes. |
| 130 | Interrupció de teclat; tracta la sortida com a incompleta. |

Per provar un altre TXG, utilitza una altra carpeta nova. Compara manifestos i
hashes abans d'unificar resultats; el programa no els barreja automàticament.

## Proves incloses

```bash
python3 -m unittest -v test_zfs_rescue
```

Les 20 proves utilitzen exclusivament fitxers temporals. Cobreixen extracció
sintètica completa, checksums, LZ4, LZJB, ZLE, gzip, endianness, SA dinàmic/spill,
dnodes grans, indirectes, offsets de partició, GPT/CRC, fallada de còpia, DVA
alternatiu, errors d'E/S complementaris, mapes ddrescue, parcials, enllaços,
protecció de rutes, sortida sense espai i invariància de les imatges originals.

Si hi ha `liblz4`, una prova contrasta el vector LZ4 amb el descodificador de la
biblioteca del sistema. Les imatges de prova estan codificades segons el format
documentat; **no s'han generat amb un pool ZFS en execució**. Consulta
`TEST_RESULTS.txt`, `AUDITORIA_CA.md` i `SOURCES.md`.

Si el lector falla amb el teu cas real, conserva els originals, els mapes, la
sortida de `scan`, el TXG seleccionat i el diagnòstic exacte. No desactivis els
checksums per obtenir una aparent recuperació satisfactòria.
