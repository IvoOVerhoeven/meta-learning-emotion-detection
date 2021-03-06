from models.seqtransformer import SeqTransformer
from modules.mlp_clf import SF_CLF
from torch import nn

# DEBUG
from transformers import AutoModel

class Transformer_CLF(nn.Module):
    """Transformer based sequence classiifer for finetuning baseline"""
    def __init__(self, args):
        super().__init__()
        self.encoder = SeqTransformer(args)
        self.classifier = SF_CLF(args.num_classes, args.hidden_dims)

    def forward(self, text, attn_mask):
        model_input = {'input_ids':text, 'attention_mask':attn_mask}
        x = self.encoder(model_input)
        logits = self.classifier(x)
        return logits
