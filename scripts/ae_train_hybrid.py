#!/usr/bin/env python3
from __future__ import annotations

import argparse, csv, json, random, sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = ROOT / "datasets" / "ae_presence"
MODEL_ROOT = ROOT / "models" / "ae_presence"
sys.path.insert(0, str(ROOT / "controller"))
from ae_oneclass import (
    FEATURES, HYBRID_MODEL_TYPE, SPECTRAL_PREPROCESSING_ID,
    DELTA_PREPROCESSING_ID, OneClassAutoencoder,
    spectral_relative_block, temporal_delta_block,
)


def load_sessions(root: Path, band: str):
    out=[]
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        mp=d/"metadata.json"
        if not mp.is_file(): continue
        try: meta=json.loads(mp.read_text(encoding="utf-8"))
        except Exception: continue
        if meta.get("status")!="ready" or meta.get("band")!=band: continue
        files=sorted((d/"samples").glob("*.npz"))
        if files: out.append((d,meta,files))
    return out


def load_raw(files):
    xs=[]
    for f in files:
        with np.load(f) as z: x=np.asarray(z["x"],dtype=np.float32)
        if x.shape!=(40,FEATURES) or not np.isfinite(x).all():
            raise RuntimeError(f"muestra inválida {f}: {x.shape}")
        xs.append(x)
    return np.stack(xs,axis=0)


def transform(raw, kind):
    fn=spectral_relative_block if kind=="spectral" else temporal_delta_block
    return np.stack([fn(x) for x in raw],axis=0).astype(np.float32)


def scores(model, blocks, mean, std, device):
    model.eval(); out=[]
    with torch.no_grad():
        for b in blocks:
            z=(b-mean)/std
            xt=torch.from_numpy(z.astype(np.float32)).to(device)
            xr=model(xt)
            row=torch.mean((xr-xt)**2,dim=1).cpu().numpy()
            out.append({"mean":float(row.mean()),"median":float(np.median(row)),"p95":float(np.percentile(row,95))})
    return out


def train_branch(name, train, val, args, device):
    rows=train.reshape(-1,FEATURES)
    mean=rows.mean(axis=0).astype(np.float32)
    std=rows.std(axis=0).astype(np.float32)
    std=np.where(std<1e-6,1.0,std).astype(np.float32)
    z=((rows-mean)/std).astype(np.float32)
    model=OneClassAutoencoder(FEATURES).to(device)
    opt=torch.optim.Adam(model.parameters(),lr=1e-3,weight_decay=1e-5)
    loss_fn=nn.MSELoss()
    loader=DataLoader(TensorDataset(torch.from_numpy(z)),batch_size=args.batch_size,shuffle=True)
    best_state=None; best_val=float("inf"); bad=0
    print(f"\n--- RAMA {name.upper()} ---")
    for epoch in range(1,args.epochs+1):
        model.train(); total=0.0; n=0
        for (xb,) in loader:
            xb=xb.to(device); opt.zero_grad(set_to_none=True)
            pred=model(xb); loss=loss_fn(pred,xb); loss.backward(); opt.step()
            total+=float(loss.item())*xb.shape[0]; n+=xb.shape[0]
        v=float(np.mean([x["mean"] for x in scores(model,val,mean,std,device)]))
        if epoch==1 or epoch%10==0:
            print(f"[{name}] epoch={epoch:03d} train_mse={total/max(n,1):.6f} val={v:.6f}")
        if v<best_val-1e-6:
            best_val=v; best_state={k:t.detach().cpu().clone() for k,t in model.state_dict().items()}; bad=0
        else:
            bad+=1
            if bad>=args.patience:
                print(f"[{name}] early-stop epoch={epoch}"); break
    if best_state is None: raise RuntimeError(f"no se obtuvo modelo {name}")
    model.load_state_dict(best_state); model.to(device)
    vals=np.asarray([x["mean"] for x in scores(model,val,mean,std,device)],dtype=float)
    base=float(np.quantile(vals,args.threshold_quantile))
    return {"model":model,"state":best_state,"mean":mean,"std":std,"base_threshold":base,"val_scores":vals}


def metrics(labels,preds):
    y=np.asarray(labels,dtype=int); p=np.asarray(preds,dtype=int)
    tp=int(((y==1)&(p==1)).sum()); tn=int(((y==0)&(p==0)).sum())
    fp=int(((y==0)&(p==1)).sum()); fn=int(((y==1)&(p==0)).sum())
    sens=tp/(tp+fn) if tp+fn else 0.0; spec=tn/(tn+fp) if tn+fp else 0.0
    return {"tp":tp,"tn":tn,"fp":fp,"fn":fn,"sensitivity":sens,"specificity":spec,"balanced_accuracy":(sens+spec)/2}


def main():
    p=argparse.ArgumentParser(description="Entrena AE híbrido espectral + temporal-delta")
    p.add_argument("--band",default="2.4g",choices=["2.4g","5g"])
    p.add_argument("--epochs",type=int,default=120)
    p.add_argument("--batch-size",type=int,default=256)
    p.add_argument("--patience",type=int,default=15)
    p.add_argument("--threshold-quantile",type=float,default=0.99)
    p.add_argument("--spectral-multiplier",type=float,default=12.0)
    p.add_argument("--delta-multiplier",type=float,default=1.25)
    p.add_argument("--seed",type=int,default=42)
    p.add_argument("--name",default="ae_24g_hybrid_v1")
    args=p.parse_args()
    if not 0.90<=args.threshold_quantile<1.0: p.error("quantile inválido")
    if args.spectral_multiplier<=1 or args.delta_multiplier<=1: p.error("multiplicadores deben ser >1")
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    sessions=load_sessions(DATASET_ROOT,args.band)
    empty=[s for s in sessions if s[1].get("condition")=="empty"]
    presence=[s for s in sessions if s[1].get("condition") in {"static","movement"}]
    if len(empty)<3: raise SystemExit(f"Se requieren >=3 sesiones EMPTY; encontré {len(empty)}")
    train_s=empty[:-2]; val_s=[empty[-2]]; test_s=[empty[-1]]
    raw_train=load_raw([f for s in train_s for f in s[2]])
    raw_val=load_raw([f for s in val_s for f in s[2]])
    raw_test=load_raw([f for s in test_s for f in s[2]])
    device=torch.device("cuda" if torch.cuda.is_available() else "cpu")
    st=transform(raw_train,"spectral"); sv=transform(raw_val,"spectral"); se=transform(raw_test,"spectral")
    dt=transform(raw_train,"delta"); dv=transform(raw_val,"delta"); de=transform(raw_test,"delta")
    spectral=train_branch("spectral",st,sv,args,device)
    delta=train_branch("delta",dt,dv,args,device)
    s_eff=spectral["base_threshold"]*args.spectral_multiplier
    d_eff=delta["base_threshold"]*args.delta_multiplier
    out=MODEL_ROOT/args.name
    if out.exists(): raise SystemExit(f"Ya existe: {out}")
    out.mkdir(parents=True)
    torch.save(spectral["state"],out/"model_spectral.pt")
    np.savez(out/"normalizer_spectral.npz",mean=spectral["mean"],std=spectral["std"])
    torch.save(delta["state"],out/"model_delta.pt")
    np.savez(out/"normalizer_delta.npz",mean=delta["mean"],std=delta["std"])
    rows=[]; labels=[]; preds=[]
    def eval_group(group,label):
        for d,meta,files in group:
            raw=load_raw(files); sb=transform(raw,"spectral"); db=transform(raw,"delta")
            ss=scores(spectral["model"],sb,spectral["mean"],spectral["std"],device)
            ds=scores(delta["model"],db,delta["mean"],delta["std"],device)
            for f,a,b in zip(files,ss,ds):
                sp=a["mean"]>s_eff; dp=b["mean"]>d_eff; pred=int(sp or dp)
                labels.append(label); preds.append(pred)
                rows.append({"session_id":meta["session_id"],"condition":meta["condition"],"label":label,"block_file":str(f),
                             "spectral_score":a["mean"],"spectral_threshold":s_eff,"spectral_ratio":a["mean"]/s_eff,
                             "delta_score":b["mean"],"delta_threshold":d_eff,"delta_ratio":b["mean"]/d_eff,
                             "prediction":pred})
    eval_group(test_s,0); eval_group(presence,1)
    m=metrics(labels,preds) if labels else {}
    metadata={
        "schema_version":2,"created_at":datetime.now(timezone.utc).isoformat(),"model_id":args.name,"band":args.band,
        "model_type":HYBRID_MODEL_TYPE,"source_feature_contract":"amplitude_3rx_52sc_200hz_ds10_40x156",
        "device_order":["rx01","rx02","rx03"],"input_features":FEATURES,"training_condition":"empty_only",
        "decision_rule":"spectral_present OR delta_present",
        "branches":{
            "spectral":{"preprocessing":SPECTRAL_PREPROCESSING_ID,"base_threshold":spectral["base_threshold"],
                        "threshold_multiplier":args.spectral_multiplier,"effective_threshold":s_eff,
                        "purpose":"presencia estática / cambio fuerte de forma espectral"},
            "delta":{"preprocessing":DELTA_PREPROCESSING_ID,"base_threshold":delta["base_threshold"],
                     "threshold_multiplier":args.delta_multiplier,"effective_threshold":d_eff,
                     "purpose":"presencia con movimiento / dinámica temporal"}},
        "threshold_rationale":{"status":"pilot_conservative_gates","note":"Multiplicadores provisionales; recalibrar con más sesiones EMPTY."},
        "train_sessions":[s[1]["session_id"] for s in train_s],"validation_sessions":[s[1]["session_id"] for s in val_s],
        "test_empty_sessions":[s[1]["session_id"] for s in test_s],"presence_evaluation_sessions":[s[1]["session_id"] for s in presence],
        "train_blocks":int(raw_train.shape[0]),"validation_blocks":int(raw_val.shape[0]),"evaluation":m}
    (out/"metadata.json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")
    with (out/"evaluation.csv").open("w",newline="",encoding="utf-8") as fh:
        fields=list(rows[0].keys()) if rows else ["session_id"]; w=csv.DictWriter(fh,fieldnames=fields); w.writeheader(); w.writerows(rows)
    print("\n"+"="*68); print(" AUTOENCODER HÍBRIDO ENTRENADO"); print("="*68)
    print(f"modelo                  : {out}")
    print(f"train EMPTY             : {len(train_s)} sesiones / {raw_train.shape[0]} bloques")
    print(f"val EMPTY               : {val_s[0][1]['session_id']} / {raw_val.shape[0]} bloques")
    print(f"test EMPTY              : {test_s[0][1]['session_id']} / {raw_test.shape[0]} bloques")
    print(f"spectral base threshold : {spectral['base_threshold']:.6f}")
    print(f"spectral effective      : {s_eff:.6f} ({args.spectral_multiplier:.2f}x)")
    print(f"delta base threshold    : {delta['base_threshold']:.6f}")
    print(f"delta effective         : {d_eff:.6f} ({args.delta_multiplier:.2f}x)")
    if m:
        print(f"balanced acc            : {m['balanced_accuracy']*100:.2f}%")
        print(f"sensibilidad            : {m['sensitivity']*100:.2f}%")
        print(f"especificidad           : {m['specificity']*100:.2f}%")
    print("="*68)

if __name__=="__main__": main()
