# Referències de format

Implementació Python original basada en la lectura del format i dels algorismes
de l'arbre **OpenZFS, etiqueta `zfs-2.3.0`**. Aquesta referència fixa evita dependre
del contingut canviant de la branca principal; no implica que es doni suport a
totes les features d'OpenZFS 2.3 o posteriors.

- [Block pointers, DVA i embedded BP](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/spa.h)
- [Descodificació dels embedded BP](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zfs/blkptr.c)
- [Etiquetes vdev i mida de ranures](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/vdev_impl.h)
- [Lectura/escriptura d'etiquetes](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zfs/vdev_label.c)
- [Uberblock](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/uberblock_impl.h)
- [Checksums incrustats i verificadors](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zfs/zio_checksum.c)
- [Byte order dels checksums SHA](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zfs/sha2_zfs.c)
- [Fletcher](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zcommon/zfs_fletcher.c)
- [LZ4](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zfs/lz4.c), [LZJB](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zfs/lzjb.c), [ZLE](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zfs/zle.c)
- [NVList XDR](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/nvpair/nvpair.c)
- [Dnode](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/dnode.h)
- [Object set](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/dmu_objset.h)
- [DSL directory](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/dsl_dir.h) i [DSL dataset](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/dsl_dataset.h)
- [Micro/fat ZAP](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/zap_impl.h), [fulles](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/zap_leaf.h) i [taula de punters](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zfs/zap.c)
- [Format SA](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/sa_impl.h), [layouts i alineament](https://github.com/openzfs/zfs/blob/zfs-2.3.0/module/zfs/sa.c), [znode antic](https://github.com/openzfs/zfs/blob/zfs-2.3.0/include/sys/zfs_sa.h)
- [GNU ddrescue: manual i mapfile](https://www.gnu.org/software/ddrescue/manual/ddrescue_manual.html)

No s'inclou codi font d'OpenZFS en aquest paquet. Les fixtures de prova són
sintètiques; no provenen d'un pool ZFS muntat ni d'una imatge dels discs de l'usuari.
