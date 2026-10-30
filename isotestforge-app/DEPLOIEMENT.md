# Déploiement gratuit et guide de test

L'application est une image Docker unique : interface web + API + simulateur ISO 8583 intégré, démarré
automatiquement sur le port 8583 du conteneur. Le runner natif l'utilise directement : aucun switch réel
n'est nécessaire pour tester.

## Quelle option choisir

| Option | Coût | Carte bancaire | Ce qui marche | Limites |
|---|---|---|---|---|
| **A. Hugging Face Spaces (Docker)** | gratuit | non | Interface complète, génération JMX, exécution sur le simulateur intégré, rapports | Pas de port TCP 8583 exposé (JMeter depuis ton PC impossible), données perdues au redémarrage, mise en veille après inactivité |
| **B. Render (Free web service)** | gratuit | non | Idem A | Idem A ; démarrage lent après 15 min d'inactivité |
| **C. VM Oracle Cloud « Always Free »** | gratuit | oui (vérification) | Tout, y compris **JMeter depuis ton PC** vers le simulateur sur le port 8583 | Création de compte plus longue |

Recommandation : **A** pour une démonstration en 10 minutes, **C** pour tester les vrais JMX avec JMeter.

Les conditions des offres gratuites évoluent : vérifier la page tarifs du fournisseur au moment du déploiement.

---

## A. Hugging Face Spaces

**Déploiement automatique depuis GitHub (sans carte, sans terminal)** : le workflow
`.github/workflows/deploy-isotestforge-hf.yml` crée le Space Docker et y publie l'application.

1. huggingface.co > **Settings > Access Tokens** : créer un jeton de type **Write**.
2. Dépôt GitHub > **Settings > Secrets and variables > Actions** :
   - secret `HF_TOKEN` = le jeton ;
   - secret `ITF_BASIC_AUTH` = `testeur:UnMotDePasseLong` (recopié comme secret du Space) ;
   - secret `ANTHROPIC_API_KEY` (facultatif) ;
   - variables facultatives `HF_SPACE` (défaut `<utilisateur>/isotestforge`) et `HF_SPACE_PRIVATE` (défaut `true`).
3. Onglet **Actions** : activer les workflows si GitHub le demande (dépôt forké), puis relancer
   *Deploy ISO-TestForge to Hugging Face Spaces*. Le journal affiche l'URL du Space.

**Déploiement manuel** :

1. Créer un compte sur huggingface.co, puis **New Space** : nom `isotestforge`, SDK **Docker**, modèle *Blank*,
   visibilité *Private* (recommandé).
2. Dans le Space : **Settings > Variables and secrets** :
   - secret `ITF_BASIC_AUTH` = `testeur:UnMotDePasseLong` (protège l'interface) ;
   - secret `ANTHROPIC_API_KEY` (facultatif ; sans clé, tout fonctionne en mode règles).
3. Sur ton poste, depuis le dossier décompressé (git et rsync installés, `huggingface-cli login` fait) :
   ```bash
   bash deploy/huggingface/prepare_space.sh <ton_utilisateur_hf> isotestforge
   ```
   Sans terminal : onglet **Files > Add file > Upload files** du Space, déposer tout le contenu du projet,
   puis ajouter en tête du `README.md` le bloc `---` présent dans `deploy/huggingface/prepare_space.sh`
   (`sdk: docker`, `app_port: 8080`).
4. Attendre la fin du build (onglet *Logs*, 3 à 5 minutes). L'application s'ouvre dans le Space.

## B. Render

1. Pousser le projet dans un dépôt GitHub (privé possible).
2. render.com > **New + > Blueprint** > choisir le dépôt : `render.yaml` est détecté (offre *Free*).
3. Renseigner `ITF_BASIC_AUTH` (et éventuellement `ANTHROPIC_API_KEY`) quand Render le demande.
4. L'URL `https://isotestforge-xxxx.onrender.com` est donnée à la fin du déploiement.

## C. VM gratuite (Oracle Cloud Always Free, ou toute VM Ubuntu)

1. Créer une instance Ubuntu 22.04/24.04 (forme *Always Free*), récupérer son IP publique.
2. Dans la **Security List** du réseau virtuel : ajouter des règles entrantes TCP **8080** et **8583**
   (idéalement limitées à ton adresse IP).
3. Copier le zip sur la VM et lancer :
   ```bash
   scp isotestforge.zip ubuntu@<IP>:~ && ssh ubuntu@<IP>
   sudo apt-get update && sudo apt-get install -y unzip && unzip isotestforge.zip && cd isotestforge
   ITF_BASIC_AUTH='testeur:UnMotDePasseLong' bash deploy/vm/install.sh
   ```
4. Interface : `http://<IP>:8080`. Simulateur ISO 8583 : `<IP>:8583`.

---

## Guide de test (20 minutes)

Ouvrir l'URL et s'identifier avec `ITF_BASIC_AUTH`.

### 1. Référentiel (2 min)
Onglet **Référentiel** : 9 cartes de test (PAN masqués), 4 terminaux, jeu de clés `KS_TEST_01` avec ses KCV,
14 mutateurs. Générer 2 cartes (BIN `476173`, type `contactless`) : elles apparaissent dans la liste.

### 2. Campagne complète depuis des besoins (8 min)
Onglet **Nouveau**, nom `DEMO`, hôte `SWITCH_SIM`, mode *Avec validations humaines*, coller :

```
REQ-100 : Le système doit accepter un achat par carte à puce EMV Visa avec saisie du PIN online. L'achat doit pouvoir être annulé par le commerçant.

REQ-101 : En cas de PIN erroné, la transaction d'achat doit être refusée.

REQ-102 : Un paiement sans contact au-delà du plafond doit exiger le PIN du porteur.

REQ-104 : Un retrait au GAB avec une carte expirée doit être refusé.

REQ-105 : Si le serveur d'autorisation ne répond pas (timeout), le terminal envoie une annulation automatique.

REQ-108 : Le switch doit rejeter un message dont le MAC est invalide.
```

| Étape | Action | Résultat attendu |
|---|---|---|
| Intentions | Lire les intentions et les questions, cliquer **Valider les intentions** | Une intention par cas ; question « canal non précisé » sur certains besoins |
| DSL | Ouvrir **Aperçu des messages** sur un scénario | DE22=051, DE52 présent, DE55 avec 9F02/9F36/9F10, crypto pin+mac+arqc |
| DSL | Cliquer **Valider le DSL** | Statut JMX ; livrables : `.jmx`, `packager.xml`, `keys.properties`, `manifest.json` |
| Exécution | Mode *Runner natif*, laisser hôte et port vides, **Exécuter** | 100 % de réussite : 00 / 55 / 54 / TIMEOUT puis 00 / 63 |
| Rapport | Ouvrir **Rapport HTML** et **Export Excel** | Statut par exigence, temps, couverture |
| Coût | Ligne « Appels agents » | « sans IA : 100 % » et 0 token si aucune clé API |

### 3. Provoquer un échec et voir le diagnostic (3 min)
Créer une nouvelle campagne avec seulement REQ-100. À l'étape DSL, ajouter sous `template: PURCHASE` :
```yaml
    expect: {"39": wrong_pin}
```
Valider puis exécuter : le scénario est **KO** (reçu 00, attendu 55) et le rapport affiche la cause probable
issue de la base de diagnostics. La correction faite dans le DSL est mémorisée (onglet **Couverture & coûts >
Corrections apprises**).

### 4. Outils (4 min)
- **Simulateur > Télécharger les traces**, puis **Outils > Import de traces** avec ce fichier :
  une suite de régression est produite, avec des cartes de test à la place des PAN.
- **Outils > Décodeur** : coller ce message ASCII (achat 0200 avec DE55) :
  `020072200400008082001647617300000000110000000000000150001030120000000123051TPE000015040149F02060000000150009F36020011`
  Résultat : champs nommés, PAN masqué, DE55 éclaté en tags EMV (9F02, 9F36). Le décodeur accepte aussi
  l'hexadécimal et le XML jPOS.
- **Couverture & coûts** : matrice carte × opération et trous (« aucune annulation sur carte sans contact »...).

### 5. JMeter réel (option C uniquement)
Sur ton PC, avec JMeter 5.6 et le plugin `jmeter-iso8583` installé (`PluginsManagerCMD install tilln-iso8583`) :
1. Télécharger depuis le workflow : le `.jmx`, `packager.xml` et `keys.properties`, dans un même dossier.
2. Lancer :
   ```bash
   jmeter -n -t DEMO.jmx -q keys.properties -JITF_HOST=<IP_VM> -JITF_PORT=8583 -JITF_PACKAGER=packager.xml -l results.jtl
   ```
   ou ouvrir le `.jmx` dans l'interface JMeter (définir les mêmes propriétés dans `user.properties`).
3. Résultat attendu : les samplers sont verts, y compris les refus attendus (55, 54, 63), car le code attendu est
   configuré comme « Success Response Code ».
4. Si l'ARQC est refusé (DE39=05 sur tous les achats EMV), passer `verify_arqc: false` dans
   `config/simulator.yaml` et relancer `docker compose up -d --build` : c'est l'écart possible entre le calcul
   jPOS de ta version et celui du simulateur.

---

## Sécurité d'une instance publique
- Toujours définir `ITF_BASIC_AUTH` ; seul `/health` reste accessible sans identifiants.
- N'utiliser que des cartes et des clés de **test** (les PAN hors `bin_whitelist.yaml` sont refusés).
- Sur la VM, restreindre 8080/8583 à ton IP dans la Security List.
