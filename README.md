# Convertisseur DAV -> MP4

**Dev by HAJJI Soufiane**

Convertisseur de fichiers `.dav` (enregistreurs Dahua/DVR) en `.mp4` compatible Windows 10/11. Interface graphique simple, detection GPU automatique, progression temps reel.

---

## Fonctionnalites

- **Remux rapide** — copie directe si le codec est H.264/MPEG-4 (tres rapide, pas de re-encodage)
- **Re-encodage H.264** — si HEVC/H.265 ou autre codec incompatible
- **GPU automatique** — NVIDIA NVENC / Intel Quick Sync / AMD AMF, detecte a chaque lancement
- **Fallback CPU** — si le GPU echoue, re-essai automatique avec le CPU
- **Progression temps reel** — pourcentage, temps restant, vitesse d'encodage
- **Validation MP4** — verification du fichier de sortie avec ffprobe
- **Annulation propre** — arret immediat sans fichier corrompu
- **Journal colore** — vert (succes), rouge (erreur), bleu (info), orange (warning)
- **Fichier de log** — automatique, utile pour le debug

---

## Utilisation

### 1. Lancer l'application

Double-cliquer sur `DAV_Converter.exe`.

> Aucune installation requise. Tout est inclus (Python, ffmpeg, ffprobe).

### 2. Ajouter des fichiers

- **"Ajouter des fichiers..."** — selectionner des fichiers `.dav` un par un
- **"Ajouter un dossier..."** — selectionner un dossier, tous les `.dav` seront ajoutes
- **"Vider la liste"** — supprimer tous les fichiers de la liste

Les fichiers sont affiches avec leur codec, resolution et taille.

### 3. Choisir le dossier de sortie

Par defaut : `C:\Utilisateurs\<votre nom>\Videos\DAV-MP4`

Cliquer sur **"Choisir..."** pour changer le dossier.

### 4. Choisir le mode d'encodage

| Mode | Description | Quand l'utiliser |
|------|-------------|------------------|
| **Auto (recommande)** | Detecte le GPU, sinon CPU rapide | La plupart du temps |
| **Forcer GPU** | Utilise obligatoirement le GPU | Si vous voulez forcer le GPU |
| **CPU rapide** | x264 preset veryfast | Si pas de GPU |
| **CPU qualite max** | x264 preset medium | Si vous voulez la meilleure qualite |

### 5. Lancer la conversion

Cliquer sur **"Demarrer la conversion"**.

Pendant la conversion, vous voyez :
- Le nom du fichier en cours
- La progression : `45%  01:05 / 02:29  (2.5x)`
- La barre de progression
- Le journal colore avec les details

### 6. Lire le resultat

Apres la conversion, cliquer sur **"Lire le MP4"** pour ouvrir le fichier avec le lecteur par defaut.

---

## Comment ca marche

### La conversion se fait en 3 etapes :

```
Fichier .dav
    │
    ▼
1. Remux (copie directe)
    │
    ├─ Codec H.264/MPEG-4 ? → Copie rapide → OK
    │
    ▼
2. Re-encodage H.264 (GPU)
    │
    ├─ Reussi ? → OK
    │
    ▼
3. Fallback CPU (si GPU echoue)
    │
    ├─ Reussi ? → OK
    │
    ▼
Echec (message d'erreur)
```

### Fichiers temporaires

Pendant la conversion, un fichier `nom.mp4.part.mp4` est cree dans le dossier de sortie.

- Si la conversion reussit → renomme en `nom.mp4`
- Si la conversion echoue → supprime automatiquement

**Aucun fichier corrompu** en cas d'arret brutal (coupure de courant, annulation, etc.).

---

## Structure des fichiers

```
dav-converter/
├── dav_to_mp4_converter.py   # Code source Python
├── dav_converter.spec        # Configuration PyInstaller
├── app_icon.ico              # Icone de l'application
├── bin/
│   ├── ffmpeg.exe            # Encodeur video (138 Mo)
│   └── ffprobe.exe           # Analyseur de flux (138 Mo)
└── dist/
    └── DAV_Converter/        # Executable pret a distribuer
        ├── DAV_Converter.exe
        └── _internal/
            └── bin/
                ├── ffmpeg.exe
                └── ffprobe.exe
```

---

## FAQ / Problemes frequent

### "FFmpeg introuvable !" (texte rouge en bas)

L'application inclut ffmpeg et ffprobe dans le dossier `bin/`. Si le message apparaît, verifiez que le dossier `bin/` contient bien `ffmpeg.exe` et `ffprobe.exe`.

### "Aucun GPU compatible"

Normal si vous n'avez pas de carte graphique NVIDIA, Intel ou AMD compatible. L'application bascule automatiquement sur le CPU. La conversion sera plus lente mais fonctionnera.

### La conversion est lente

- Verifiez le mode d'encodage : **Auto** est le plus rapide
- Les fichiers HEVC 4K prennent plus de temps que les H.264
- Le **remux** (copie directe) est quasi instantane — regardez le codec dans la liste

### Le journal ne s'affiche pas pendant la conversion

C'est normal. Le journal s'affiche a la fin. Pendant la conversion, regardez la **barre de progression** et le **pourcentage** en haut.

### Comment recompiler l'executable ?

```bash
pip install pyinstaller
py -m PyInstaller --clean -y dav_converter.spec
```

L'executable sera dans `dist\DAV_Converter\`.

### Comment modifier le code ?

Ouvrez `dav_to_mp4_converter.py` avec n'importe quel editeur de texte (Notepad++, VS Code, etc.). Puis recompilez avec la commande ci-dessus.

---

## Developpement

### Dependances

- Python 3.11+ (inclus dans l'executable via PyInstaller)
- tkinter (inclus avec Python)
- ffmpeg / ffprobe (inclus dans `bin/`)

### Compiler

```bash
# Installer PyInstaller
pip install pyinstaller

# Compiler
py -m PyInstaller --clean -y dav_converter.spec
```

### Structure du code

| Section | Description |
|---------|-------------|
| 1. CONSTANTES | Codec, modes d'encodage, arguments GPU/CPU |
| 2. UTILITAIRES | Formatage, detection de outils |
| 3. FFPROBE | Analyse des flux video/audio |
| 4. GPU DETECTION | Test des encodeurs GPU |
| 5. FFMPEG RUNNER | Execution de ffmpeg, parsing de la progression |
| 6. VALIDATION | Verification du fichier MP4 de sortie |
| 7. CONVERSION | Logique de remux, re-encodage, fallback |
| 8. WORKER | orchestration de la conversion par lots |
| 9. GUI | Interface graphique Tkinter |
| 10. MAIN | Point d'entree |

---

## Credit

**Dev by HAJJI Soufiane**

---

## Licence

Usage libre et gratuit.
