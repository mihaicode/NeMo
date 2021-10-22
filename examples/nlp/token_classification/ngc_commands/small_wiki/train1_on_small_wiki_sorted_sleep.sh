WANDB_API_KEY="$1"

read -r -d '' command << EOF
set -e -x
OMP_NUM_THREADS=8
git clone https://github.com/NVIDIA/NeMo
mkdir -p /result/nemo_experiments
cd NeMo
git checkout feat/sorted_punct_dataset
source reinstall.sh
cd examples/nlp/token_classification
wandb login ${WANDB_API_KEY}
python -c "from nemo.collections.nlp.modules import get_tokenizer;get_tokenizer('bert-base-uncased', use_fast=False)"
sleep 200000
set +e +x
EOF

ngc batch run \
  --instance dgx1v.16g.1.norm \
  --name "ml-model.bert sorted_punctuation_capitalization_training_on_small_wiki" \
  --image "nvidia/pytorch:21.08-py3" \
  --result /result \
  --datasetid 90228:/data \
  --commandline "${command}"