"""Terminal setup only: never opens sources or creates the destination."""
from __future__ import annotations

import argparse
import os
import stat
import sys

from .base import Unsupported
from .tui import label


class SetupCancelled(Exception):
    pass


def _ask(prompt, default=""):
    try:
        answer = input(prompt).strip()
    except EOFError as exc:
        raise SetupCancelled from exc
    if answer.lower() == "q":
        raise SetupCancelled
    return answer or default


def _choice(prompt, choices, default=""):
    while True:
        answer = _ask(prompt, default).lower()
        if answer in choices:
            return answer
        print("Tria una de les opcions indicades, o q per cancel·lar.")


def _path(prompt, *, optional=False, mapfile=False):
    while True:
        answer = _ask(prompt)
        if not answer and optional:
            return None
        if not answer:
            print("Cal indicar una ruta.")
            continue
        path = os.path.expanduser(answer)
        try:
            mode = os.stat(path).st_mode
            valid = stat.S_ISREG(mode) or (not mapfile and
                                          (stat.S_ISBLK(mode) or stat.S_ISCHR(mode)))
            if not valid:
                raise ValueError("La ruta no és un fitxer d'imatge o un dispositiu admès"
                                 if not mapfile else "El mapa ha de ser un fitxer regular")
        except (OSError, ValueError) as exc:
            print("Ruta no vàlida: " + label(str(exc)))
            continue
        return path


def _bytes(prompt, integer, *, positive=False, aligned=False):
    while True:
        try:
            value = integer(_ask(prompt))
            if positive and value == 0:
                raise ValueError("La longitud ha de ser superior a zero")
            if aligned and value % 512:
                raise ValueError("L'offset ha de ser múltiple de 512 bytes")
            return value
        except (argparse.ArgumentTypeError, ValueError) as exc:
            print("Valor no vàlid: " + str(exc))


def _destination():
    while True:
        path = _ask("Destí NOU al disc sa (ruta completa): ")
        if not path:
            print("Cal indicar una ruta.")
            continue
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.lexists(path):
            print("El destí ja existeix. Tria un nom nou, fins i tot si és buit.")
        elif not os.path.isdir(os.path.dirname(path)):
            print("La carpeta pare no existeix. Munta primer el disc sa.")
        else:
            return path


def configure(args, integer):
    """Fill missing select arguments; leave supplied CLI values untouched."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Unsupported("L'assistent necessita un terminal interactiu (TTY). "
                          "Utilitza select amb --source, --dataset i --dest per passar arguments.")
    print("\nAssistent de rescat ZFS — mirror de dos membres (experimental)")
    print("Conserva els originals; preferiblement treballa amb imatges.")
    print("No importis ni muntis el pool original, ni facis scrub o reparacions.")
    print("Si un disc fa sorolls o es desconnecta, atura't.")
    print("q cancel·la; Ctrl+C interromp. Escriu les rutes sense cometes, també amb espais.")

    if not args.source:
        print("\nIdentifica les fonts per model i número de sèrie amb lsblk i /dev/disk/by-id/.")
        print("No confonguis la partició ZFS amb swap ni un disc complet amb una partició.")
        sources, offsets, lengths, maps = [], [], [], []
        for index in range(2):
            while True:
                path = _path(f"Font {index + 1}" + (" (Enter si només tens un membre)" if index else "") + ": ",
                             optional=index == 1)
                if path is None or os.path.realpath(path) not in [os.path.realpath(source) for source in sources]:
                    break
                # Source identity is also checked after opening, including aliases/hardlinks.
                print("Aquesta font ja s'ha indicat. Tria l'altre membre, o Enter per ometre'l.")
            if path is None:
                print("Un sol membre: no hi haurà una segona còpia per rescatar blocs.")
                break
            kind = _choice("Tipus: 1=partició ZFS/imatge de partició, "
                           "2=disc complet/imatge de disc [1]: ", ("1", "2"), "1")
            offset, length = 0, None
            if kind == "2":
                print("Calen els límits coneguts de la partició ZFS, EN BYTES.")
                print("Si no els saps, cancel·la: «python3 zfs_rescue.py probe --source RUTA»")
                print("mostra la GPT; per sectors lògics de 4096 afegeix --sector-size 4096.")
                print("No inventis els límits ni continuïs si les dues GPT discrepen.")
                offset = _bytes("Offset de la partició ZFS en bytes: ", integer, aligned=True)
                length = _bytes("Longitud de la partició ZFS en bytes: ", integer, positive=True)
            print("El mapa ddrescue, si en tens, ha de correspondre exactament a aquesta font.")
            mapfile = _path("Mapa ddrescue (Enter si no en tens): ", optional=True, mapfile=True)
            sources.append(path)
            offsets.append(offset)
            lengths.append(length)
            maps.append(mapfile or "-")
        args.source, args.offset, args.length, args.mapfile = sources, offsets, lengths, maps

    if not args.dataset:
        args.dataset = _ask("\nDataset exacte [vol1/DATA]: ", "vol1/DATA")
    if not args.dest:
        print("\nEl destí ha de ser en un disc sa FÍSICAMENT INDEPENDENT.")
        print("Comprova que estigui muntat i tingui espai; crear una carpeta no munta un disc.")
        args.dest = _destination()

    print("\nRevisa la configuració (encara no s'ha obert cap font ni creat cap destí):")
    for index, path in enumerate(args.source):
        offset = args.offset[index] if args.offset else 0
        length = args.length[index] if args.length else None
        mapfile = args.mapfile[index] if args.mapfile else "-"
        print(f"  Font {index + 1}: {label(path)}; offset={offset}; "
              f"longitud={length if length is not None else 'fins al final'}; mapa={label(mapfile)}")
    print("  Dataset: " + label(args.dataset))
    print("  Destí nou: " + label(args.dest))
    if _choice("Obrir el selector amb aquesta configuració? [s/N]: ", ("s", "n"), "n") != "s":
        raise SetupCancelled
    print("Ara es validarà el pool. No es copiarà res fins que marquis rutes i confirmis al selector.")
