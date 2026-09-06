<?xml version="1.0" encoding="UTF-8"?>
<!-- Trimmed to the shape that matters for `pubRxStatus`, keeping the real
     namespace prefix and element names SEL Architect writes. REL_SUB
     subscribes to two publishers: one whose subscription declares a health
     bit (VB051) and one that declares none. REL_PUB1 declares a health bit
     on a remote bit (RB11) instead of a virtual bit, which is the other
     shape the corpus carries.

     `pubRxStatus` is the declared fact. The ExtRef twin of a health bit
     carries a control block and NO doName/daName: an exact proxy across
     the corpus, asserted by the tests but deliberately not a fallback in
     the reader. -->
<SCL xmlns="http://www.iec.ch/61850/2003/SCL"
     xmlns:esel="http://www.selinc.com/2005/SCL"
     version="2007" revision="B">
  <Communication>
    <SubNetwork name="SbNet1">
      <ConnectedAP iedName="REL_PUB1" apName="S1">
        <GSE ldInst="CFG" cbName="GoSB00">
          <Address>
            <P type="MAC-Address">01-0C-CD-01-00-33</P>
            <P type="APPID">0330</P>
            <P type="VLAN-ID">033</P>
            <P type="VLAN-PRIORITY">4</P>
          </Address>
        </GSE>
      </ConnectedAP>
      <ConnectedAP iedName="REL_PUB2" apName="S1">
        <GSE ldInst="CFG" cbName="GoSB01">
          <Address>
            <P type="MAC-Address">01-0C-CD-01-00-35</P>
            <P type="APPID">0350</P>
            <P type="VLAN-ID">035</P>
            <P type="VLAN-PRIORITY">4</P>
          </Address>
        </GSE>
      </ConnectedAP>
    </SubNetwork>
  </Communication>

  <IED name="REL_SUB" type="SEL_451_5" manufacturer="SEL">
    <Private type="SEL_GooseSubscription">
      <!-- This section is overwritten by Architect and regenerated from information in the <Inputs> elements. -->
      <esel:GooseSubscription iedName="REL_PUB1" ldInst="CFG" cbName="GoSB00"
                              datSet="GOPB_138" goCbRef="REL_PUB1CFG/LLN0$GO$GoSB00"
                              pubRxStatus="VB051" confRev="1"
                              datSetRef="REL_PUB1CFG/LLN0$GOPB_138">
        <esel:GooseRxEntry tdlString="{(VB011$r0)Bool,(r0$o0x1807)BVstring13,Utctime}"/>
      </esel:GooseSubscription>
      <!-- No pubRxStatus: a subscription whose health is not mapped to a bit. -->
      <esel:GooseSubscription iedName="REL_PUB2" ldInst="CFG" cbName="GoSB01"
                              datSet="GOPB_139" goCbRef="REL_PUB2CFG/LLN0$GO$GoSB01"
                              confRev="1" datSetRef="REL_PUB2CFG/LLN0$GOPB_139">
        <esel:GooseRxEntry tdlString="{(VB012$r0)Bool,(r0$o0x1807)BVstring13,Utctime}"/>
      </esel:GooseSubscription>
    </Private>
    <AccessPoint name="S1">
      <Server>
        <LDevice inst="CFG">
          <LN0 lnType="LN0" lnClass="LLN0" inst="">
            <Inputs>
              <!-- A data subscription: names a DO and a DA. -->
              <ExtRef desc="52A LT2" serviceType="GOOSE" iedName="REL_PUB1" intAddr="VB011"
                      srcLDInst="CFG" srcLNClass="LLN0" srcCBName="GoSB00"
                      ldInst="CFG" lnClass="LLN0" doName="Ind01" daName="stVal"/>
              <!-- The health bit of that same subscription: control block, no DO/DA.
                   Carries serviceType="GOOSE", as in samples/IEC station 1.scd, so
                   it IS one of the subscriptions the reader returns. -->
              <ExtRef desc="FALHA GOOSE LT2" serviceType="GOOSE" iedName="REL_PUB1" intAddr="VB051"
                      srcLDInst="CFG" srcLNClass="LLN0" srcCBName="GoSB00"/>
              <ExtRef desc="52A LT3" serviceType="GOOSE" iedName="REL_PUB2" intAddr="VB012"
                      srcLDInst="CFG" srcLNClass="LLN0" srcCBName="GoSB01"
                      ldInst="CFG" lnClass="LLN0" doName="Ind01" daName="stVal"/>
              <!-- A placeholder: no publisher, no control block. -->
              <ExtRef intAddr="VB099"/>
            </Inputs>
          </LN0>
        </LDevice>
      </Server>
    </AccessPoint>
  </IED>

  <IED name="REL_PUB1" type="SEL_487E" manufacturer="SEL">
    <Private type="SEL_GooseSubscription">
      <esel:GooseSubscription iedName="REL_SUB" ldInst="CFG" cbName="GPub01"
                              datSet="URDSet07" goCbRef="REL_SUBCFG/LLN0$GO$GPub01"
                              pubRxStatus="RB11" confRev="1"
                              datSetRef="REL_SUBCFG/LLN0$URDSet07">
        <esel:GooseRxEntry tdlString="{(RB01$r0)Bool,(r0$o0x1807)BVstring13,Utctime}"/>
      </esel:GooseSubscription>
    </Private>
    <AccessPoint name="S1">
      <Server>
        <LDevice inst="CFG">
          <LN0 lnType="LN0" lnClass="LLN0" inst="">
            <Inputs>
              <!-- The other shape: no serviceType at all, as in
                   samples/substation_demo.scd, where all 202 health ExtRefs
                   omit it and are therefore not returned as subscriptions. -->
              <ExtRef desc="FALHA GOOSE SUB" iedName="REL_SUB" intAddr="RB11"
                      srcLDInst="CFG" srcLNClass="LLN0" srcCBName="GPub01"/>
            </Inputs>
          </LN0>
        </LDevice>
      </Server>
    </AccessPoint>
  </IED>

  <!-- No Private block at all: an SCD from another tool, or one Architect
       never regenerated. Must yield no health bits and raise nothing. -->
  <IED name="REL_PLAIN" type="SEL_751" manufacturer="SEL">
    <AccessPoint name="S1">
      <Server>
        <LDevice inst="CFG">
          <LN0 lnType="LN0" lnClass="LLN0" inst="">
            <Inputs>
              <ExtRef desc="52A" serviceType="GOOSE" iedName="REL_PUB1" intAddr="VB001"
                      srcLDInst="CFG" srcLNClass="LLN0" srcCBName="GoSB00"
                      ldInst="CFG" lnClass="LLN0" doName="Ind01" daName="stVal"/>
            </Inputs>
          </LN0>
        </LDevice>
      </Server>
    </AccessPoint>
  </IED>
</SCL>
