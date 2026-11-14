# Auditoria del codi proporcionat

> Les seccions originals següents descriuen la versió modular inicial.
> L'ampliació de selecció es documenta al final; la identitat AST històrica
> no s'aplica als mètodes modificats ara.

La versió original és un esbós de lectura del MOS, no un rescatador de datasets.
No l'he considerat adequada per executar-la com a única via de recuperació sobre
discs físicament degradats.

| Punt | Codi original | Canvi en aquesta versió |
|---|---|---|
| Objectiu real | Desa `mos_txg_*.bin`; no recorre cap dataset. | Implementa MOS, DSL, ZPL, dnodes, indirectes, ZAP, SA i extracció. |
| Sectors perduts | Retorna zeros després d'errors o lectures fora de límits. | Genera errors explícits; només els holes verificats es tracten com a zeros legítims. |
| Corrupció silenciosa | Si `pread` retorna prou bytes, no consulta el mirror. | Comprova checksum i prova l'altre membre, i altres DVA, quan no coincideix. |
| Uberblocks | Comprova només el número màgic i llegeix 1024 bytes. | Verifica la ranura sencera, el checksum incrustat, l'offset i el byte order. |
| Selecció TXG | Prioritza el més recent; `TARGET_TXG` absent pot acabar retornant un altre TXG. | Selecció estricta amb `--txg`; alternativa automàtica només quan no es fixa un valor exacte. |
| Geometria | `ASHIFT=12` fix; offsets referits al disc complet. | Llegeix ashift de configuracions verificades i permet offset/longitud de cada partició. |
| Identitat mirror | No comprova pool GUID, membre o vdev. | Valida la pertinença al mateix mirror i rebutja membres duplicats/topologies incompatibles. |
| DVA | Només DVA0; ignora vdev i indicador gang. | Prova DVA alternatius, verifica vdev ID i rebutja gang explícitament. |
| Embedded BP | LSIZE usa 24 bits i PSIZE comença al bit 24. | Usa els 25 bits de LSIZE i PSIZE al bit 25; descodifica el payload per paraules. |
| Compressió | Tracta `inherit=0` com a sense compressió. | No inventa una interpretació de codis no resolts o desconeguts. |
| LZ4 | Reconeix el prefix de 4 bytes però manca validació integral. | Valida longituds, límits, mida lògica i checksum del bloc físic. |
| Fitxers incomplets | No hi ha cap distinció perquè no extreu fitxers. | Separa parcials, registra intervals perduts i publica només fitxers complets a l'arbre. |
| Sortida | Carpeta reutilitzable, fitxer obert amb `wb`, log al directori actual. | Carpeta nova, creació exclusiva, informe a la destinació i diagnòstics a stderr. |
| Proves | No se n'adjunten. | 20 proves sintètiques i d'injecció d'errors; no equivalen a validació amb un pool real. |

`os.O_RDONLY` i `os.pread` del programa original són una bona base. El canvi no
els substitueix: afegeix comprovacions al voltant de la lectura i evita confondre
un buffer llegit amb un bloc correcte.

## Què continua sense estar demostrat

No s'ha observat el contingut dels discs de l'usuari ni s'ha verificat el seu
pool, la versió concreta de TrueNAS/OpenZFS, les features utilitzades, el xifrat,
la integritat dels noms o la proporció de blocs recuperables. Tampoc no s'ha
executat aquest programa en FreeBSD o contra dispositius físics.

La preservació exacta de l'arbre depèn de poder llegir les metadades que relacionen
els noms amb els objectes. Tenir el contingut físic d'un fitxer no és suficient
per reconstruir-ne el camí original quan aquestes relacions s'han perdut. En
aquest cas el programa registra la fallada i no inventa noms o ubicacions.

El suport de formats indicat a `README_CA.md` descriu la implementació, no una
certificació d'interoperabilitat amb totes les versions de ZFS. Abans de confiar
en el resultat d'un cas real, cal contrastar una mostra de fitxers i el manifest
amb dades conegudes o amb una eina de recuperació que llegeixi aquell mateix pool.

## Verificació de la divisió en mòduls

S'han comparat els arbres de sintaxi de les 36 funcions i classes amb els de
la versió monolítica: els cossos són idèntics. El paquet utilitza importacions
relatives dins de `zfs_reader` i una entrada `zfs_rescue.py`. S'han actualitzat
les importacions de les proves i el punt d'injecció de l'error ENOSPC.
Les 19 proves anteriors i una prova nova d'execució des d'una altra carpeta
superen la validació en Linux amb Python 3.12. Això verifica les fixtures i
la reorganització; continuen vigents els límits de validació amb pools reals.

## Ampliació: navegació i recuperació selectiva

- `selection.py` valida rutes relatives per components i impedeix travessar
  symlinks. `browse.py` recorre metadades, sense llegir dades de fitxers.
- `--include` filtra abans d'encuar entrades; una selecció no recorre els
  subarbres germans. L'extracció per ordres exigeix TXG explícit; el selector
  conserva el mateix object set/TXG des de la navegació fins a la còpia.
  Les rutes es comproven abans de crear cap destinació. Checksum, rèpliques,
  fonts `O_RDONLY` i protecció de destinació original no s'han relaxat.
- S'afegeixen límits de directori i pressupostos de rutes pendents/enllaços
  durs per reduir el risc de consumir memòria sense límit. Són pressupostos
  d'estructures concretes, no un límit RSS global ni una prova en 8 GB.
- Les proves noves portables utilitzen dobles d'E/S i també les fixtures ZFS
  sintètiques originals. Verifiquen bytes, hashes de fonts, selecció, parcials,
  fallback de checksum, byte order i offsets. No demostren comportament del
  kernel POSIX ni interoperabilitat amb pools reals.
- Les proves POSIX originals i quatre casos d'integració nous queden
  disponibles per executar en Linux. L'execució actual en Windows les marca
  com a omeses. Vegeu `TEST_RESULTS.txt` per als resultats exactes.

## Selector interactiu de terminal

- `tui.py` afegeix `select`: Enter només obre directoris; Espai marca/desmarca
  carpetes senceres o fitxers individuals. No obre continguts de fitxers ni
  segueix symlinks. Les marques es mantenen en canviar de carpeta i s'unifiquen
  per components, sense confondre noms semblants.
- La revisió amb `r` exigeix una confirmació diferent (`s`). Una selecció buida
  no pot convertir-se en una extracció completa. Cancel·lar, interrompre o
  excedir un pressupost abans de confirmar no crea la destinació.
- Només manté un directori, 256 entrades d'atributs en memòria cau i marques amb
  pressupost estimat de 64 MiB. No construeix un catàleg del dataset ni visita
  descendents per marcar una carpeta. Les rutes llargues es poden consultar
  completes; els caràcters de control i noms no UTF-8 es mostren escapats.
- `curses` s'importa sota demanda, amb comprovació de terminal abans d'obrir
  fonts. La pantalla i el nivell de log es restauren també davant d'errors.
  L'extracció posterior reutilitza la destinació segura i el manifest existents.
- `test_tui.py` afegeix proves portables de model, teclat/pantalla, CLI i imatge
  sintètica, i dues proves curses/PTY POSIX. Aquestes dues, juntament amb les
  24 proves POSIX anteriors, queden pendents d'execució en Linux.
